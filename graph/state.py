"""LangGraph 状态 — 配置选型流程。"""

from __future__ import annotations

from typing import Any, TypedDict


class QuotationState(TypedDict, total=False):
    inquiry_id: str
    thread_id: str
    raw_text: str
    user_reply: str
    slots: dict[str, Any]
    clarify_round: int
    clarify_question: str
    missing_slots: list[str]
    draft_config: dict[str, Any]
    applied_experiences: list[dict[str, Any]]
    learned_experiences: list[str]
    status: str
    human_config: dict[str, Any]
    configuration_id: str
    error: str
