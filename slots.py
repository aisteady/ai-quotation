"""
槽位抽取（slots.py）— 五要素 + 选型增强字段。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from schemas import InquirySlots

logger = logging.getLogger(__name__)


def merge_slots(base: InquirySlots, patch: InquirySlots) -> InquirySlots:
    data = base.model_dump()
    for k, v in patch.model_dump(exclude_none=True).items():
        if k == "extra":
            data["extra"] = {**(data.get("extra") or {}), **(v or {})}
        elif v is not None and v != "":
            data[k] = v
    return InquirySlots.model_validate(data)


def _capture(patterns: list[str], text: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            val = (m.group(1) or "").strip()
            if val:
                return val
    return None


def heuristic_extract(text: str) -> InquirySlots:
    t = text.strip()
    slots = InquirySlots()

    slots.material = _capture(
        [
            r"(?:加工)?物料(?:是|为|：|:)?\s*([^\s,，；;。]{1,40})",
            r"(?:原料|粉料|物料名称)\s*[:：]?\s*([^\s,，；;。]{1,40})",
        ],
        t,
    )
    slots.fineness = _capture(
        [
            r"(?:成品)?(?:要求)?细度\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"(?:出料|成品)\s*(?:粒度|细度)\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"(\d+\s*目)",
            r"(D\s*50\s*[≤<>=]?\s*\d+(?:\.\d+)?\s*(?:μm|um|微米)?)",
        ],
        t,
    )
    # 注意：d95/D95 属于通筛率（通过率），不要当成细度
    slots.sieve_pass_rate = _capture(
        [
            r"通筛率\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"(?:过筛率|筛余|通过率)\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"(?:通筛|过筛)\s*[≥>=]?\s*(\d+(?:\.\d+)?\s*%)",
            r"(d\s*95\s*[≥>=≤<=]?\s*\d+(?:\.\d+)?\s*%?)",
            r"(D\s*95\s*[≥>=≤<=]?\s*\d+(?:\.\d+)?\s*%?)",
        ],
        t,
    )
    slots.capacity = _capture(
        [
            r"产量\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"(?:处理量|产能|产能要求)\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"(\d+(?:\.\d+)?\s*(?:t/h|T/H|吨/?时|kg/h|千克/?时|吨/?天))",
        ],
        t,
    )
    slots.feed_size = _capture(
        [
            r"(?:进料|入料|喂料)(?:尺寸|粒度|粒径)?\s*[:：]?\s*([^\s,，；;。]{1,40})",
            r"物料进入机器时(?:为|是|：|:)?\s*([^\s,，；;。]{1,40})",
            r"(?:进料|入料)\s*[≤<]?\s*(\d+(?:\.\d+)?\s*(?:mm|毫米|cm|目))",
        ],
        t,
    )
    slots.moisture = _capture(
        [r"(?:含水|湿度|水分)\s*[:：]?\s*([^\s,，；;。]{1,30})"],
        t,
    )
    slots.hardness = _capture(
        [r"(?:硬度|莫氏|磨蚀|易碎|韧性)\s*[:：]?\s*([^\s,，；;。]{1,40})"],
        t,
    )
    slots.power_supply = _capture(
        [r"(?:电源|电压)\s*[:：]?\s*([^\s,，；;。]{1,40})", r"(380\s*V[^,，；;。]{0,20})"],
        t,
    )
    slots.equipment_name = _capture(
        [
            r"(?:设备|机型|型号|工艺)\s*[:：]?\s*([^\s,，；;。]{2,40})",
            r"((?:超微|气流|球磨|雷蒙|锤式)?粉碎机|[A-Za-z0-9\-]+型)",
        ],
        t,
    )
    m_qty = re.search(r"(?:采购|订|要)?\s*(\d+(?:\.\d+)?)\s*(?:台|套|台套)", t, re.I)
    if m_qty:
        slots.equipment_qty = float(m_qty.group(1))
    m_cust = re.search(r"(?:客户|公司)\s*[:：]?\s*([^\s,，。]{2,40})", t)
    if m_cust:
        slots.customer_name = m_cust.group(1)
    return slots


class SlotLLM:
    def __init__(
        self,
        project_id: str,
        model: str | None = None,
    ) -> None:
        self.project_id = (project_id or "").strip()
        self.model = (model or "").strip() or None

    @property
    def available(self) -> bool:
        return bool(self.project_id)

    def extract(self, text: str, prior: InquirySlots | None = None) -> InquirySlots:
        if not self.available:
            return merge_slots(prior or InquirySlots(), heuristic_extract(text))

        prior_json = (prior or InquirySlots()).model_dump_json()
        prompt = (
            "你是粉体设备选型助手。从用户话中抽取 JSON，不要编造价格。\n"
            "必填: material, fineness, sieve_pass_rate, capacity, feed_size\n"
            "术语：细度=目数或 D50 等粒度；d95/D95=通筛率（通过某筛的质量占比），"
            "必须写入 sieve_pass_rate，禁止当作 fineness。\n"
            "选填: equipment_name, equipment_qty, moisture, hardness, power_supply, "
            "customer_name, delivery_days, notes\n"
            "保留用户原单位。无则 null。\n"
            f"已有: {prior_json}\n用户: {text}\n只输出 JSON。"
        )
        try:
            from mcp_llm import chat_via_mcp

            raw = chat_via_mcp(
                project_id=self.project_id,
                prompt=prompt,
                model=self.model,
            )
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return merge_slots(prior or InquirySlots(), heuristic_extract(text))
            data: dict[str, Any] = json.loads(m.group(0))
            patch = InquirySlots.model_validate(data)
            return merge_slots(
                merge_slots(prior or InquirySlots(), heuristic_extract(text)), patch
            )
        except Exception as exc:
            logger.warning("LLM 抽槽失败，回退规则: %s", exc)
            return merge_slots(prior or InquirySlots(), heuristic_extract(text))
