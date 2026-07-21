"""
配置选型经验：人审反馈 → 归纳规则 → 下次生成召回套用
====================================================

可解释打分（关键词 Jaccard + 槽位提示），非向量检索。
LLM 负责把自然语言修订意见整理成结构化经验；失败则走启发式。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from schemas import (
    AppliedConfigExperience,
    ConfigActionType,
    ConfigExperienceRecord,
    ConfigLineItem,
    ConfigurationProposal,
    InquirySlots,
)
from store import QuotationStore

logger = logging.getLogger(__name__)

# 纯确认、无修订价值的短句 —— 不学习
_TRIVIAL_FEEDBACK = re.compile(
    r"^(可以|同意|确认|ok|okay|好的|没问题|通过|是的|行|嗯)+[\s。.!！]*$",
    re.I,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _tokenize(text: str) -> set[str]:
    parts = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", (text or "").lower())
    return {p for p in parts if len(p) >= 2}


def _slots_blob(slots: InquirySlots) -> str:
    return " ".join(
        x
        for x in [
            slots.process_summary(),
            slots.material,
            slots.fineness,
            slots.sieve_pass_rate,
            slots.capacity,
            slots.feed_size,
            slots.moisture,
            slots.hardness,
            slots.equipment_name,
            slots.notes,
        ]
        if x
    )


def _proposal_blob(proposal: ConfigurationProposal | None) -> str:
    if not proposal:
        return ""
    bits = [proposal.title, proposal.overview, proposal.process_basis]
    for it in proposal.line_items:
        bits.append(f"{it.category} {it.name} {it.model_spec} {it.remark}")
    return " ".join(bits)


def is_learnable_feedback(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 6:
        return False
    # 去掉「可以」等前缀后再判
    cleaned = re.sub(
        r"^(可以|同意|确认)[，,。\s]*",
        "",
        t,
        flags=re.I,
    ).strip()
    if not cleaned:
        return False
    if _TRIVIAL_FEEDBACK.match(cleaned):
        return False
    # 必须含有修订意味或设备/工艺词
    markers = (
        "不用",
        "不必",
        "不需要",
        "去掉",
        "删除",
        "取消",
        "应该",
        "注意",
        "改成",
        "改为",
        "破碎",
        "分级",
        "除尘",
        "细度",
        "通筛",
        "d95",
        "入料",
        "进料",
        "磨机",
        "主机",
        "功率",
        "风量",
    )
    low = cleaned.lower()
    return any(m in low for m in markers) or len(cleaned) >= 20


def similarity_score(
    slots: InquirySlots,
    proposal: ConfigurationProposal | None,
    exp: ConfigExperienceRecord,
) -> float:
    """条件文本 Jaccard + 槽位提示加分。"""
    a = _tokenize(_slots_blob(slots) + " " + _proposal_blob(proposal))
    b = _tokenize(
        " ".join(
            [
                exp.condition_summary,
                exp.rule_text,
                exp.title,
                " ".join(exp.condition_keywords),
                " ".join(exp.slot_hints.values()),
                exp.action_target,
            ]
        )
    )
    score = 0.0
    if a and b:
        score += 0.7 * (len(a & b) / len(a | b))

    # 槽位提示：经验提到的槽在当前需求里有值 → 加分
    hint_hits = 0
    hint_total = 0
    for key, hint in (exp.slot_hints or {}).items():
        hint_total += 1
        cur = getattr(slots, key, None) if hasattr(slots, key) else None
        if cur is None or not str(cur).strip():
            continue
        cur_s = str(cur)
        hint_toks = _tokenize(hint) | {hint} if hint else set()
        if not hint:
            hint_hits += 1
            continue
        if any(h in cur_s for h in hint_toks if len(h) >= 1) or any(
            t in cur_s for t in _tokenize(hint)
        ):
            hint_hits += 1
        elif key in ("feed_size", "fineness", "capacity") and cur_s:
            # 有对应槽位即可算半命中（规则常写「入料已细」）
            hint_hits += 0.5

    if hint_total:
        score += 0.25 * (hint_hits / hint_total)

    # 动作目标已出现在当前清单 → 更相关（尤其 remove_line）
    if proposal and exp.action_type == ConfigActionType.REMOVE_LINE and exp.action_target:
        targets = [t.strip() for t in exp.action_target.split("|") if t.strip()]
        blob = _proposal_blob(proposal).lower()
        if any(t.lower() in blob for t in targets):
            score += 0.2

    return min(score, 1.0)


def apply_experience_to_proposal(
    proposal: ConfigurationProposal,
    exp: ConfigExperienceRecord,
) -> tuple[ConfigurationProposal, bool]:
    """对单条经验执行动作；返回 (新方案, 是否改动)。"""
    changed = False
    if exp.action_type == ConfigActionType.REMOVE_LINE:
        targets = [t.strip().lower() for t in exp.action_target.split("|") if t.strip()]
        if not targets:
            return proposal, False
        kept: list[ConfigLineItem] = []
        for it in proposal.line_items:
            blob = f"{it.category} {it.name} {it.model_spec} {it.remark}".lower()
            if any(t in blob for t in targets):
                changed = True
                continue
            kept.append(it)
        if changed:
            for i, it in enumerate(kept, start=1):
                it.item_no = i
            proposal.line_items = kept
            tip = f"已套用经验「{exp.title or exp.id[:8]}」：移除含 {exp.action_target} 的配置项。"
            if tip not in proposal.warnings:
                proposal.warnings.append(tip)

    elif exp.action_type == ConfigActionType.ADD_WARNING:
        msg = (exp.action_payload or {}).get("message") or exp.rule_text or exp.title
        if msg and msg not in proposal.warnings:
            proposal.warnings.append(msg)
            changed = True

    elif exp.action_type == ConfigActionType.SET_PARAM:
        key = exp.action_target or (exp.action_payload or {}).get("key") or ""
        val = str((exp.action_payload or {}).get("value") or "")
        if key and val:
            proposal.equipment_params.params[key] = val
            tip = f"已套用经验「{exp.title or exp.id[:8]}」：参数 {key}={val}"
            if tip not in proposal.warnings:
                proposal.warnings.append(tip)
            changed = True

    elif exp.action_type == ConfigActionType.NOTE:
        tip = f"经验参考「{exp.title or exp.id[:8]}」：{exp.rule_text or exp.condition_summary}"
        if tip not in proposal.warnings:
            proposal.warnings.append(tip)
            changed = True

    return proposal, changed


def heuristic_extract_experiences(
    feedback: str,
    slots: InquirySlots,
    *,
    inquiry_id: str | None = None,
) -> list[ConfigExperienceRecord]:
    """无 LLM 时的规则抽取（覆盖破碎机/通筛术语等常见反馈）。"""
    now = _utc_now()
    out: list[ConfigExperienceRecord] = []
    fb = feedback.strip()
    low = fb.lower()

    if any(k in fb for k in ("破碎", "预碎", "颚破", "锤破")) and any(
        k in fb for k in ("不用", "不必", "不需要", "去掉", "删除", "取消", "无需")
    ):
        out.append(
            ConfigExperienceRecord(
                id=str(uuid.uuid4()),
                title="入料已达磨机要求则不配破碎机",
                rule_text=(
                    "当客户入料尺寸已满足磨机/主机入料要求时，配置清单中不增加破碎机。"
                ),
                condition_summary="入料细度/尺寸已符合磨机所需入料，无需预破碎",
                condition_keywords=["入料", "进料", "破碎", "磨机", "细度"],
                slot_hints={"feed_size": slots.feed_size or "已达磨机入料"},
                action_type=ConfigActionType.REMOVE_LINE,
                action_target="破碎|预碎|颚破|锤破",
                feedback_raw=fb,
                source_inquiry_id=inquiry_id,
                created_at=now,
                updated_at=now,
            )
        )

    if ("d95" in low or "D95" in fb) and any(
        k in fb for k in ("通筛", "不是细度", "非细度", "细度是")
    ):
        out.append(
            ConfigExperienceRecord(
                id=str(uuid.uuid4()),
                title="d95 属于通筛率不是细度",
                rule_text="d95/D95 表示通筛率（通过某筛的质量占比），细度用目数或 D50。",
                condition_summary="需求或方案中出现 d95/D95 与细度混淆",
                condition_keywords=["d95", "通筛", "细度"],
                slot_hints={"sieve_pass_rate": "d95"},
                action_type=ConfigActionType.ADD_WARNING,
                action_target="terminology",
                action_payload={
                    "message": (
                        "术语提醒：D95/d95 为通筛率，不是细度；"
                        "细度请用目数或 D50。"
                    )
                },
                feedback_raw=fb,
                source_inquiry_id=inquiry_id,
                created_at=now,
                updated_at=now,
            )
        )

    # 兜底：有实质反馈但未命中专用规则 → 记一条 NOTE
    if not out and is_learnable_feedback(fb):
        out.append(
            ConfigExperienceRecord(
                id=str(uuid.uuid4()),
                title=(fb[:24] + "…") if len(fb) > 24 else fb,
                rule_text=fb,
                condition_summary=slots.process_summary(),
                condition_keywords=list(_tokenize(fb + " " + slots.process_summary()))[:12],
                slot_hints={
                    k: str(getattr(slots, k))
                    for k in ("material", "feed_size", "fineness", "capacity")
                    if getattr(slots, k, None)
                },
                action_type=ConfigActionType.NOTE,
                action_target="",
                feedback_raw=fb,
                source_inquiry_id=inquiry_id,
                created_at=now,
                updated_at=now,
            )
        )
    return out


def llm_extract_experiences(
    feedback: str,
    slots: InquirySlots,
    proposal: ConfigurationProposal | None,
    *,
    project_id: str = "",
    model: str | None = None,
    inquiry_id: str | None = None,
) -> list[ConfigExperienceRecord]:
    if not (project_id or "").strip():
        return []

    lines = []
    if proposal:
        for it in proposal.line_items[:12]:
            lines.append(f"{it.item_no}.{it.category}/{it.name}")
    prompt = (
        "你是粉体设备选型工程师。把人工审核意见归纳为 0~3 条可复用经验，输出 JSON 数组。\n"
        "每条字段: title, rule_text, condition_summary, condition_keywords(数组),\n"
        "slot_hints(对象,可选), action_type(remove_line|add_warning|set_param|note),\n"
        "action_target(remove_line 时为设备关键词用|分隔), action_payload(对象,可选)。\n"
        "只提取有明确配置影响的规则；纯确认不要输出。\n"
        f"工艺条件: {slots.process_summary()}\n"
        f"当前清单: {'; '.join(lines)}\n"
        f"人审意见: {feedback}\n"
        "只输出 JSON 数组。"
    )
    try:
        from mcp_llm import chat_via_mcp

        raw = chat_via_mcp(
            project_id=project_id,
            prompt=prompt,
            model=(model or "").strip() or None,
        )
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return []
        data = json.loads(m.group(0))
        if not isinstance(data, list):
            return []
        now = _utc_now()
        out: list[ConfigExperienceRecord] = []
        for item in data[:3]:
            if not isinstance(item, dict):
                continue
            at = str(item.get("action_type") or "note")
            try:
                action = ConfigActionType(at)
            except ValueError:
                action = ConfigActionType.NOTE
            out.append(
                ConfigExperienceRecord(
                    id=str(uuid.uuid4()),
                    title=str(item.get("title") or "")[:80],
                    rule_text=str(item.get("rule_text") or "")[:500],
                    condition_summary=str(item.get("condition_summary") or "")[:300],
                    condition_keywords=[
                        str(x) for x in (item.get("condition_keywords") or []) if x
                    ][:20],
                    slot_hints={
                        str(k): str(v)
                        for k, v in (item.get("slot_hints") or {}).items()
                    },
                    action_type=action,
                    action_target=str(item.get("action_target") or ""),
                    action_payload=dict(item.get("action_payload") or {}),
                    feedback_raw=feedback,
                    source_inquiry_id=inquiry_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        return out
    except Exception as exc:
        logger.warning("LLM 归纳经验失败: %s", exc)
        return []


class ConfigExperienceService:
    def __init__(
        self,
        store: QuotationStore,
        *,
        apply_threshold: float = 0.35,
        project_id: str = "",
        llm_model: str | None = None,
    ) -> None:
        self.store = store
        self.apply_threshold = apply_threshold
        self.project_id = (project_id or "").strip()
        self.llm_model = (llm_model or "").strip() or None

    def retrieve_and_apply(
        self,
        slots: InquirySlots,
        proposal: ConfigurationProposal,
        *,
        top_k: int = 5,
    ) -> tuple[ConfigurationProposal, list[AppliedConfigExperience]]:
        """生成后召回匹配经验并改写清单。"""
        candidates = self.store.list_active_experiences(limit=200)
        scored: list[tuple[float, ConfigExperienceRecord]] = []
        for exp in candidates:
            if not self._condition_compatible(slots, exp):
                continue
            s = similarity_score(slots, proposal, exp)
            if s >= self.apply_threshold:
                scored.append((s, exp))
        scored.sort(key=lambda x: x[0], reverse=True)

        applied: list[AppliedConfigExperience] = []
        for s, exp in scored[:top_k]:
            proposal, changed = apply_experience_to_proposal(proposal, exp)
            if changed:
                self.store.bump_experience_hit(exp.id)
                applied.append(
                    AppliedConfigExperience(
                        experience_id=exp.id,
                        title=exp.title,
                        similarity=round(s, 4),
                        action_type=exp.action_type,
                        rule_text=exp.rule_text,
                    )
                )
        return proposal, applied

    @staticmethod
    def _condition_compatible(slots: InquirySlots, exp: ConfigExperienceRecord) -> bool:
        """
        硬约束：避免「细入料不加破碎」被套到粗入料单上。
        """
        if exp.action_type != ConfigActionType.REMOVE_LINE:
            return True
        if not any(k in (exp.action_target or "") for k in ("破碎", "预碎", "颚破")):
            return True
        feed = slots.feed_size or ""
        m = re.search(r"(\d+(?:\.\d+)?)", feed)
        if not m:
            return True
        mm = float(m.group(1))
        # 经验语义是「入料已够细」；粗入料（>15mm）不自动去破碎
        return mm <= 15.0

    def learn_from_feedback(
        self,
        *,
        inquiry_id: str,
        slots: InquirySlots,
        draft: ConfigurationProposal | None,
        final: ConfigurationProposal,
        feedback: str,
    ) -> list[ConfigExperienceRecord]:
        """
        人审意见 → 结构化经验落库，并立即把可执行动作套到本单 final。
        返回新写入的经验列表。
        """
        if not is_learnable_feedback(feedback):
            return []

        extracted = llm_extract_experiences(
            feedback,
            slots,
            final or draft,
            project_id=self.project_id,
            model=self.llm_model,
            inquiry_id=inquiry_id,
        )
        if not extracted:
            extracted = heuristic_extract_experiences(
                feedback, slots, inquiry_id=inquiry_id
            )
        if not extracted:
            return []

        saved: list[ConfigExperienceRecord] = []
        for exp in extracted:
            # 去重：同 title + action_type + action_target 已存在则 bump 不重复插
            existing = self.store.find_similar_experience(
                title=exp.title,
                action_type=exp.action_type.value,
                action_target=exp.action_target,
            )
            if existing:
                # 用新反馈刷新 rule_text / keywords
                existing.rule_text = exp.rule_text or existing.rule_text
                existing.condition_summary = (
                    exp.condition_summary or existing.condition_summary
                )
                existing.condition_keywords = list(
                    dict.fromkeys(
                        (existing.condition_keywords or [])
                        + (exp.condition_keywords or [])
                    )
                )[:20]
                existing.feedback_raw = feedback
                existing.updated_at = _utc_now()
                if exp.slot_hints:
                    existing.slot_hints = {**existing.slot_hints, **exp.slot_hints}
                self.store.upsert_experience(existing)
                apply_experience_to_proposal(final, existing)
                saved.append(existing)
            else:
                self.store.upsert_experience(exp)
                apply_experience_to_proposal(final, exp)
                saved.append(exp)
        return saved
