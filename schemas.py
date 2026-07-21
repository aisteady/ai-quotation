"""
AI 工艺选型配置 — 领域模型（schemas.py）
========================================

主目标：根据工艺五要素及其他信息，生成
  1) 详细配置清单（BOM / 配套清单）
  2) 主机及关键设备的配置参数

人审确认的是「配置方案」，不是单价（单价可作为选填备注）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class InquiryStatus(str, Enum):
    """需求单生命周期。"""

    DRAFT = "draft"
    NEED_CLARIFY = "need_clarify"
    PENDING_HUMAN = "pending_human"  # 待人审确认配置方案
    CONFIGURED = "configured"  # 已确认配置清单与参数
    CANCELLED = "cancelled"
    FAILED = "failed"


REQUIRED_SLOTS = (
    "material",
    "fineness",
    "sieve_pass_rate",
    "capacity",
    "feed_size",
)

SLOT_LABELS: dict[str, str] = {
    "material": "加工物料（是什么物料）",
    "fineness": "成品要求细度（目数 / D50 等粒度指标，不是 d95）",
    "sieve_pass_rate": "通筛率（含 d95/D95：通过某筛的质量占比）",
    "capacity": "产量",
    "feed_size": "进料尺寸（物料进入机器时的尺寸）",
    "equipment_name": "倾向机型/工艺路线（选填）",
    "equipment_qty": "主机套数（选填）",
    "moisture": "物料含水/湿度（选填）",
    "hardness": "物料硬度/特性（选填）",
    "power_supply": "电源工况（选填，如 380V/50Hz）",
    "customer_name": "客户名称（选填）",
    "notes": "其他要求（选填）",
}


class InquirySlots(BaseModel):
    """需求槽位：五要素必填，其余增强选型。"""

    material: str | None = None
    fineness: str | None = None
    sieve_pass_rate: str | None = None
    capacity: str | None = None
    feed_size: str | None = None

    equipment_name: str | None = None
    equipment_qty: float | None = None
    moisture: str | None = None
    hardness: str | None = None
    power_supply: str | None = None
    customer_name: str | None = None
    delivery_days: int | None = None
    notes: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def missing_required(self) -> list[str]:
        missing: list[str] = []
        for key in REQUIRED_SLOTS:
            val = getattr(self, key, None)
            if val is None or not str(val).strip():
                missing.append(key)
        return missing

    def clarify_prompt(self) -> str:
        miss = self.missing_required()
        if not miss:
            return ""
        parts = [SLOT_LABELS.get(m, m) for m in miss]
        return (
            "生成配置清单前还需确认以下工艺参数，请补充："
            + "、".join(parts)
            + "。"
        )

    def process_summary(self) -> str:
        bits = [
            f"物料={self.material or '?'}",
            f"细度={self.fineness or '?'}",
            f"通筛率={self.sieve_pass_rate or '?'}",
            f"产量={self.capacity or '?'}",
            f"进料尺寸={self.feed_size or '?'}",
        ]
        if self.moisture:
            bits.append(f"含水={self.moisture}")
        if self.hardness:
            bits.append(f"特性={self.hardness}")
        if self.equipment_name:
            bits.insert(0, f"倾向机型={self.equipment_name}")
        return "；".join(bits)


class ConfigLineItem(BaseModel):
    """配置清单中的一行。"""

    item_no: int = 1
    category: str = ""  # 主机/分级/除尘/给料/电气/管道附件…
    name: str = ""
    model_spec: str = ""
    quantity: float = 1
    unit: str = "台"
    remark: str = ""


class EquipmentParams(BaseModel):
    """
    设备配置参数（键值结构化，便于人审改数）。

    常见键：主电机功率、转速、分级轮转速、风量、处理量、进料粒度、
    出料细度、通筛率目标、压缩空气、外形尺寸等。
    """

    host_model: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class ConfigurationProposal(BaseModel):
    """系统根据五要素生成的配置方案（人审前可改）。"""

    title: str = "工艺选型配置方案"
    overview: str = ""
    line_items: list[ConfigLineItem] = Field(default_factory=list)
    equipment_params: EquipmentParams = Field(default_factory=EquipmentParams)
    process_basis: str = ""  # 五要素摘要
    kb_snippets: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    caution: str = "以下为系统初稿配置，须人工审核后方可作为正式配置清单。"


class HumanConfigInput(BaseModel):
    """
    人审确认配置。

    - reject=True：作废本方案
    - 否则：可用 proposal 覆盖系统初稿（允许改清单行与参数）
    - rationale 中的修订意见会在 finalize 时归纳为选型经验
    """

    reject: bool = False
    reject_reason: str | None = None
    rationale: str = ""
    proposal: ConfigurationProposal | None = None


class ConfigActionType(str, Enum):
    """经验可执行的配置动作。"""

    REMOVE_LINE = "remove_line"  # 去掉匹配的清单行（如破碎机）
    ADD_WARNING = "add_warning"  # 写入 warnings
    SET_PARAM = "set_param"  # 改写设备参数某键
    NOTE = "note"  # 仅记经验，生成时提示


class ConfigExperienceRecord(BaseModel):
    """
    人审反馈沉淀的选型经验。

    例：入料已达磨机要求 → 不配置破碎机
    （condition + action，下次同类条件自动套用）
    """

    id: str
    title: str = ""
    rule_text: str = ""
    condition_summary: str = ""
    condition_keywords: list[str] = Field(default_factory=list)
    # 可选：与槽位相关的软条件提示，如 {"feed_size": "小|细|<=10"}
    slot_hints: dict[str, str] = Field(default_factory=dict)
    action_type: ConfigActionType = ConfigActionType.NOTE
    # remove_line：类别/名称关键词，用 | 分隔；set_param：参数键名
    action_target: str = ""
    action_payload: dict[str, Any] = Field(default_factory=dict)
    feedback_raw: str = ""
    source_inquiry_id: str | None = None
    version: int = 1
    parent_experience_id: str | None = None
    is_active: bool = True
    hit_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class AppliedConfigExperience(BaseModel):
    """某次生成中命中并套用的经验摘要。"""

    experience_id: str
    title: str = ""
    similarity: float = 0.0
    action_type: ConfigActionType = ConfigActionType.NOTE
    rule_text: str = ""


class ConfigurationRecord(BaseModel):
    """已确认的正式配置（落库）。"""

    id: str
    inquiry_id: str
    title: str = ""
    overview: str = ""
    line_items_json: str = "[]"
    equipment_params_json: str = "{}"
    process_basis: str = ""
    rationale: str = ""
    created_by: str = "human"
    created_at: str = ""


class InquiryRecord(BaseModel):
    id: str
    thread_id: str
    status: InquiryStatus = InquiryStatus.DRAFT
    raw_text: str = ""
    slots: InquirySlots = Field(default_factory=InquirySlots)
    clarify_round: int = 0
    draft_config: ConfigurationProposal | None = None
    configuration_id: str | None = None
    trace_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
