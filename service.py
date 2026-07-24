"""
工艺选型配置 — 编排门面 + Harness
================================

对外目标：五要素齐全后生成「配置清单 + 设备参数」，经人审确认落库。

  start_inquiry(text)
  resume_clarify(thread, reply)
  resume_human_config(thread, payload)   # 确认/修订/拒绝配置方案
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from config import settings, validate_settings
from config_engine import build_configuration, enrich_overview_with_llm, format_proposal_text
from experience import ConfigExperienceService
from graph.build import build_graph
from harness import HarnessLoop, QuotationHarness
from schemas import SLOT_LABELS, InquirySlots
from slots import SlotLLM, heuristic_extract, merge_slots
from store import QuotationStore

logger = logging.getLogger(__name__)


class QuotationService:
    def __init__(self, *, start_harness_loop: bool = True) -> None:
        validate_settings(strict=False)
        self.store = QuotationStore()
        self.harness = QuotationHarness(self.store)
        self._loop = HarnessLoop(self.store, self.harness)
        self.experience = ConfigExperienceService(
            self.store,
            apply_threshold=settings.experience_apply_threshold,
            project_id=settings.project_id,
            llm_model=settings.llm_model or None,
        )

        extract_fn = self._make_extract_fn()
        search_fn = self._make_search_fn()
        self.graph = build_graph(
            self.store,
            self.harness,
            extract_fn=extract_fn,
            search_fn=search_fn,
            experience=self.experience,
            project_id=settings.project_id,
            llm_model=settings.llm_model or None,
        )
        if start_harness_loop:
            self._loop.start()

    def _make_extract_fn(self):
        llm = None
        if settings.project_id and settings.mcp_client_token:
            llm = SlotLLM(
                settings.project_id,
                model=settings.llm_model or None,
            )

        def extract(text: str, prior: InquirySlots | None) -> InquirySlots:
            if llm and llm.available:
                return llm.extract(text, prior)
            return merge_slots(prior or InquirySlots(), heuristic_extract(text))

        return extract

    def _make_search_fn(self):
        if not settings.project_id or not settings.mcp_client_token:
            return None

        def search(query: str) -> list[dict[str, Any]]:
            from mcp_client import build_mcp_client

            client = build_mcp_client()
            raw = client.call_tool(
                "search_documents",
                {
                    "query": query,
                    "project_id": settings.project_id,
                    "top_k": settings.top_k,
                    "threshold": settings.search_threshold,
                },
            )
            text = str(raw or "").strip()
            if not text or "未找到" in text:
                return []
            return [{"text": text[:2000]}]

        return search

    def start_inquiry(self, text: str, *, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id or str(uuid.uuid4())
        inquiry_id = str(uuid.uuid4())

        def _run() -> dict[str, Any]:
            config = {"configurable": {"thread_id": tid}}
            self.graph.invoke(
                {
                    "inquiry_id": inquiry_id,
                    "thread_id": tid,
                    "raw_text": text,
                    "user_reply": text,
                    "clarify_round": 0,
                    "slots": {},
                },
                config=config,
            )
            return self._pack(tid)

        return self.harness.wrap_run("start_inquiry", _run, inquiry_id=inquiry_id)

    def resume_clarify(self, thread_id: str, user_reply: str) -> dict[str, Any]:
        if not (user_reply or "").strip():
            raise ValueError("补充内容不能为空")

        def _run() -> dict[str, Any]:
            from langgraph.types import Command

            config = {"configurable": {"thread_id": thread_id}}
            self.graph.invoke(
                Command(resume={"user_reply": user_reply.strip()}),
                config=config,
            )
            return self._pack(thread_id)

        return self.harness.wrap_run("resume_clarify", _run)

    def resume_human_config(
        self, thread_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """人审确认配置清单与设备参数。"""

        def _run() -> dict[str, Any]:
            from langgraph.types import Command

            config = {"configurable": {"thread_id": thread_id}}
            self.graph.invoke(Command(resume=payload), config=config)
            return self._pack(thread_id)

        return self.harness.wrap_run("resume_human_config", _run)

    # 兼容旧名
    def resume_human_quote(
        self, thread_id: str, quote: dict[str, Any]
    ) -> dict[str, Any]:
        return self.resume_human_config(thread_id, quote)

    def recommend_for_customer(
        self,
        *,
        query: str = "",
        extras: str = "",
        slots: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        供智能客服同步调用：抽槽 → 校验五要素 → 生成配置方案正文。

        不走 LangGraph 人审 interrupt；缺参时返回 missing，由客服 Clarify 追问。
        """
        prior = InquirySlots.model_validate(slots or {})
        blob = "\n".join(x for x in [(query or "").strip(), (extras or "").strip()] if x)
        extract = self._make_extract_fn()
        merged = extract(blob, prior) if blob else prior
        if slots:
            # 显式 slots 覆盖抽槽结果（多轮客服回传的已填项）
            merged = merge_slots(merged, InquirySlots.model_validate(slots))

        missing = merged.missing_required()
        slots_partial = merged.model_dump(mode="json", exclude_none=True)
        if missing:
            labels = [SLOT_LABELS.get(m, m) for m in missing]
            return {
                "ok": False,
                "error": "missing_required_slots",
                "missing": missing,
                "missing_labels": labels,
                "clarify_question": merged.clarify_prompt(),
                "slots_partial": slots_partial,
                "proposal_text": "",
                "structured": None,
            }

        snippets: list[dict[str, Any]] = []
        search = self._make_search_fn()
        if search:
            q = " ".join(
                x
                for x in [
                    merged.material,
                    merged.fineness,
                    merged.capacity,
                    merged.equipment_name,
                    "配置 选型",
                ]
                if x
            ).strip()
            if q:
                try:
                    snippets = search(q)[: settings.top_k]
                except Exception as exc:
                    logger.warning("recommend 检索失败: %s", exc)

        proposal = build_configuration(merged, kb_snippets=snippets)
        if settings.project_id:
            proposal = enrich_overview_with_llm(
                proposal,
                project_id=settings.project_id,
                model=settings.llm_model or None,
            )
        applied_meta: list[dict[str, Any]] = []
        try:
            proposal, applied = self.experience.retrieve_and_apply(merged, proposal)
            applied_meta = [a.model_dump(mode="json") for a in applied]
        except Exception as exc:
            logger.warning("recommend 套用经验失败: %s", exc)

        text = format_proposal_text(proposal)
        self.harness.emit(
            "customer_recommend_ok",
            lines=len(proposal.line_items),
            material=merged.material,
        )
        return {
            "ok": True,
            "error": "",
            "missing": [],
            "missing_labels": [],
            "clarify_question": "",
            "slots_partial": slots_partial,
            "proposal_text": text,
            "structured": proposal.model_dump(mode="json"),
            "applied_experiences": applied_meta,
        }

    def get_state(self, thread_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        snap = self.graph.get_state(config)
        values = dict(snap.values or {})
        interrupted = bool(snap.next)
        payload = None
        if snap.tasks:
            for t in snap.tasks:
                if getattr(t, "interrupts", None):
                    ints = t.interrupts
                    if ints:
                        payload = ints[0].value
                        break
        return {
            "thread_id": thread_id,
            "interrupted": interrupted,
            "next": list(snap.next or []),
            "values": values,
            "interrupt_payload": payload,
        }

    def _pack(self, thread_id: str) -> dict[str, Any]:
        state = self.get_state(thread_id)
        values = state.get("values") or {}
        return {
            "thread_id": thread_id,
            "inquiry_id": values.get("inquiry_id"),
            "status": values.get("status"),
            "interrupted": state.get("interrupted"),
            "interrupt_payload": state.get("interrupt_payload"),
            "values": values,
            "configuration_id": values.get("configuration_id"),
            "learned_experiences": values.get("learned_experiences") or [],
            "applied_experiences": values.get("applied_experiences") or [],
            "error": values.get("error"),
        }

    def stop(self) -> None:
        self._loop.stop()
