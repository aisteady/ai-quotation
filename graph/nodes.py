"""
配置选型图节点。

parse_slots → clarify ↺ → generate_config → human_review → finalize
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from config_engine import build_configuration, enrich_overview_with_llm
from experience import ConfigExperienceService
from harness import QuotationHarness
from schemas import (
    REQUIRED_SLOTS,
    ConfigurationProposal,
    ConfigurationRecord,
    HumanConfigInput,
    InquiryRecord,
    InquirySlots,
    InquiryStatus,
)
from store import QuotationStore

from graph.state import QuotationState


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class NodeContext:
    store: QuotationStore
    harness: QuotationHarness
    extract_fn: Callable[[str, InquirySlots | None], InquirySlots]
    search_fn: Callable[[str], list[dict[str, Any]]] | None = None
    experience: ConfigExperienceService | None = None
    project_id: str = ""
    llm_api_key: str = ""  # 兼容旧字段，已忽略
    llm_model: str | None = None


def make_nodes(ctx: NodeContext) -> dict[str, Any]:
    def parse_slots(state: QuotationState) -> dict[str, Any]:
        prior = InquirySlots.model_validate(state.get("slots") or {})
        text = (state.get("user_reply") or state.get("raw_text") or "").strip()
        if not text:
            return {
                "error": "空输入",
                "status": InquiryStatus.FAILED.value,
                "missing_slots": list(REQUIRED_SLOTS),
            }

        slots = ctx.extract_fn(text, prior)
        raw = state.get("raw_text") or ""
        if state.get("user_reply") and state.get("user_reply") not in raw:
            raw = (raw + "\n" + state["user_reply"]).strip()

        inquiry_id = state.get("inquiry_id") or str(uuid.uuid4())
        miss = slots.missing_required()
        ctx.store.add_message(inquiry_id, "user", text)

        # 尽早落库，便于 HarnessLoop / 运营看见补全中单据
        rec = InquiryRecord(
            id=inquiry_id,
            thread_id=state.get("thread_id") or inquiry_id,
            status=(
                InquiryStatus.NEED_CLARIFY if miss else InquiryStatus.DRAFT
            ),
            raw_text=raw or text,
            slots=slots,
            clarify_round=int(state.get("clarify_round") or 0),
            draft_config=(
                ConfigurationProposal.model_validate(state["draft_config"])
                if state.get("draft_config")
                else None
            ),
            configuration_id=state.get("configuration_id"),
            updated_at=_utc_now(),
            created_at=_utc_now(),
        )
        old = ctx.store.get_inquiry(inquiry_id)
        if old:
            rec.created_at = old.created_at
        ctx.store.upsert_inquiry(rec)

        return {
            "inquiry_id": inquiry_id,
            "raw_text": raw or text,
            "slots": slots.model_dump(mode="json"),
            "missing_slots": miss,
            "user_reply": "",
            "status": (
                InquiryStatus.NEED_CLARIFY.value if miss else InquiryStatus.DRAFT.value
            ),
        }

    def route_completeness(state: QuotationState) -> str:
        if state.get("status") == InquiryStatus.FAILED.value and state.get("error"):
            return "end_fail"
        miss = state.get("missing_slots") or []
        return "clarify" if miss else "generate_config"

    def clarify(state: QuotationState) -> dict[str, Any]:
        from langgraph.types import interrupt

        round_n = int(state.get("clarify_round") or 0) + 1
        ok, msg = ctx.harness.guard_clarify_round(round_n)
        if not ok:
            ctx.harness.emit(
                "clarify_limit", inquiry_id=state.get("inquiry_id"), round=round_n
            )
            return {
                "clarify_round": round_n,
                "status": InquiryStatus.FAILED.value,
                "error": msg,
                "clarify_question": msg,
            }

        slots = InquirySlots.model_validate(state.get("slots") or {})
        question = slots.clarify_prompt()
        ctx.store.add_message(state["inquiry_id"], "assistant", question)
        ctx.harness.emit(
            "clarify_ask",
            inquiry_id=state.get("inquiry_id"),
            round=round_n,
            missing=state.get("missing_slots") or [],
        )

        resume = interrupt(
            {
                "type": "clarify",
                "inquiry_id": state.get("inquiry_id"),
                "question": question,
                "missing_slots": state.get("missing_slots") or [],
                "slots": state.get("slots") or {},
                "round": round_n,
                "hint": "请补充工艺参数；resume: {user_reply: ...}",
            }
        )
        reply = ""
        if isinstance(resume, str):
            reply = resume
        elif isinstance(resume, dict):
            reply = str(resume.get("user_reply") or resume.get("text") or "")
        return {
            "clarify_round": round_n,
            "user_reply": reply,
            "clarify_question": question,
            "status": InquiryStatus.NEED_CLARIFY.value,
        }

    def generate_config(state: QuotationState) -> dict[str, Any]:
        """五要素齐全 → 生成配置清单 + 设备参数。"""
        slots = InquirySlots.model_validate(state.get("slots") or {})
        snippets: list[dict[str, Any]] = []
        query = " ".join(
            x
            for x in [
                slots.material,
                slots.fineness,
                slots.capacity,
                slots.equipment_name,
                "配置 选型",
            ]
            if x
        ).strip()
        if ctx.search_fn and query:
            try:
                from config import settings

                snippets = ctx.search_fn(query)[: settings.top_k]
            except Exception as exc:
                snippets = [{"error": str(exc)}]

        proposal = build_configuration(slots, kb_snippets=snippets)
        if ctx.project_id:
            proposal = enrich_overview_with_llm(
                proposal, project_id=ctx.project_id, model=ctx.llm_model
            )

        applied_meta: list[dict[str, Any]] = []
        if ctx.experience:
            proposal, applied = ctx.experience.retrieve_and_apply(slots, proposal)
            applied_meta = [a.model_dump(mode="json") for a in applied]
            if applied:
                ctx.harness.emit(
                    "experience_applied",
                    inquiry_id=state.get("inquiry_id"),
                    count=len(applied),
                    ids=[a.experience_id for a in applied],
                )

        ctx.harness.emit(
            "config_generated",
            inquiry_id=state.get("inquiry_id"),
            lines=len(proposal.line_items),
        )
        return {
            "draft_config": proposal.model_dump(mode="json"),
            "applied_experiences": applied_meta,
            "status": InquiryStatus.PENDING_HUMAN.value,
        }

    def human_review(state: QuotationState) -> dict[str, Any]:
        from langgraph.types import interrupt

        inquiry_id = state["inquiry_id"]
        draft = ConfigurationProposal.model_validate(state.get("draft_config") or {})
        rec = InquiryRecord(
            id=inquiry_id,
            thread_id=state.get("thread_id") or inquiry_id,
            status=InquiryStatus.PENDING_HUMAN,
            raw_text=state.get("raw_text") or "",
            slots=InquirySlots.model_validate(state.get("slots") or {}),
            clarify_round=int(state.get("clarify_round") or 0),
            draft_config=draft,
            updated_at=_utc_now(),
            created_at=_utc_now(),
        )
        old = ctx.store.get_inquiry(inquiry_id)
        if old:
            rec.created_at = old.created_at
        ctx.store.upsert_inquiry(rec)
        ctx.harness.emit("await_human_config", inquiry_id=inquiry_id)

        resume = interrupt(
            {
                "type": "human_config",
                "inquiry_id": inquiry_id,
                "slots": state.get("slots") or {},
                "draft_config": draft.model_dump(mode="json"),
                "hint": "提交 HumanConfigInput：可带修订后的 proposal，或 reject=true",
            }
        )
        if isinstance(resume, dict):
            return {
                "human_config": resume,
                "status": InquiryStatus.PENDING_HUMAN.value,
            }
        return {
            "error": "人审载荷无效",
            "status": InquiryStatus.FAILED.value,
        }

    def finalize(state: QuotationState) -> dict[str, Any]:
        inquiry_id = state["inquiry_id"]
        raw = state.get("human_config") or {}
        human = HumanConfigInput.model_validate(raw)
        draft = ConfigurationProposal.model_validate(state.get("draft_config") or {})

        if human.reject:
            rec = ctx.store.get_inquiry(inquiry_id)
            if rec:
                rec.status = InquiryStatus.CANCELLED
                rec.updated_at = _utc_now()
                ctx.store.upsert_inquiry(rec)
            ctx.store.add_message(
                inquiry_id, "system", f"配置方案已拒绝: {human.reject_reason or ''}"
            )
            ctx.harness.emit("config_reject", inquiry_id=inquiry_id)
            return {"status": InquiryStatus.CANCELLED.value}

        final = human.proposal or draft
        # 人审修订意见 → 归纳经验，并立即改写本单清单（如去掉破碎机）
        learned_ids: list[str] = []
        if ctx.experience and (human.rationale or "").strip():
            slots = InquirySlots.model_validate(state.get("slots") or {})
            learned = ctx.experience.learn_from_feedback(
                inquiry_id=inquiry_id,
                slots=slots,
                draft=draft,
                final=final,
                feedback=human.rationale,
            )
            learned_ids = [e.id for e in learned]
            if learned:
                ctx.harness.emit(
                    "experience_learned",
                    inquiry_id=inquiry_id,
                    count=len(learned),
                    ids=learned_ids,
                    titles=[e.title for e in learned],
                )

        cid = str(uuid.uuid4())
        record = ConfigurationRecord(
            id=cid,
            inquiry_id=inquiry_id,
            title=final.title,
            overview=final.overview,
            line_items_json=final.model_dump_json(),  # 整包；下面拆字段更清晰
            equipment_params_json=final.equipment_params.model_dump_json(),
            process_basis=final.process_basis,
            rationale=human.rationale,
            created_by="human",
            created_at=_utc_now(),
        )
        # line_items 单独存数组 JSON
        import json

        record.line_items_json = json.dumps(
            [x.model_dump() for x in final.line_items], ensure_ascii=False
        )
        ctx.store.save_configuration(record)

        rec = ctx.store.get_inquiry(inquiry_id)
        if rec:
            rec.status = InquiryStatus.CONFIGURED
            rec.configuration_id = cid
            rec.draft_config = final
            rec.updated_at = _utc_now()
            ctx.store.upsert_inquiry(rec)

        msg = f"配置方案已确认：{final.title}（{len(final.line_items)} 项清单）。"
        if learned_ids:
            msg += f" 已沉淀 {len(learned_ids)} 条选型经验。"
        ctx.store.add_message(inquiry_id, "assistant", msg)
        ctx.harness.emit(
            "configured",
            inquiry_id=inquiry_id,
            configuration_id=cid,
            lines=len(final.line_items),
            experiences_learned=len(learned_ids),
        )
        return {
            "configuration_id": cid,
            "draft_config": final.model_dump(mode="json"),
            "learned_experiences": learned_ids,
            "status": InquiryStatus.CONFIGURED.value,
        }

    def end_fail(state: QuotationState) -> dict[str, Any]:
        return {"status": InquiryStatus.FAILED.value}

    def route_after_clarify(state: QuotationState) -> str:
        if state.get("status") == InquiryStatus.FAILED.value:
            return "end_fail"
        return "parse_slots"

    return {
        "parse_slots": parse_slots,
        "route_completeness": route_completeness,
        "route_after_clarify": route_after_clarify,
        "clarify": clarify,
        "generate_config": generate_config,
        "human_review": human_review,
        "finalize": finalize,
        "end_fail": end_fail,
    }
