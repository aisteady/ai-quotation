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
from experience import ConfigExperienceService
from graph.build import build_graph
from harness import HarnessLoop, QuotationHarness
from schemas import InquirySlots
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
