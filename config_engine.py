"""
配置方案生成（config_engine.py）
================================

根据工艺五要素 + 选填信息，生成：
  - 详细配置清单（主机、分级、除尘、给料、风机、电气等）
  - 设备配置参数表

规则引擎保证无 LLM 也能出完整清单；有知识库片段时写入参考；
可选 LLM 润色 overview（失败则忽略）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from schemas import (
    ConfigLineItem,
    ConfigurationProposal,
    EquipmentParams,
    InquirySlots,
)

logger = logging.getLogger(__name__)


def _parse_capacity_tph(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(t/h|T/H|吨/?时)", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg/h|千克/?时)", text, re.I)
    if m:
        return float(m.group(1)) / 1000.0
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _suggest_host(slots: InquirySlots) -> tuple[str, str]:
    """返回 (工艺路线名, 主机型号建议)。"""
    name = (slots.equipment_name or "").strip()
    if name:
        return name, name
    fin = (slots.fineness or "").lower()
    # 粗粒度启发：超细倾向气流/分级圈，中等细度锤式/球磨示意
    if any(k in fin for k in ("超微", "微米", "μm", "um", "d50")) or "气流" in name:
        return "气流粉碎 + 分级闭路", "QLF-建议型（待人审核定）"
    if "目" in fin:
        m = re.search(r"(\d+)\s*目", fin)
        mesh = int(m.group(1)) if m else 0
        if mesh >= 800:
            return "气流粉碎 + 高精度分级", "QLF-建议型（待人审核定）"
        if mesh >= 200:
            return "机械粉碎 + 分级", "CFJ-建议型（待人审核定）"
        return "粗碎/中碎工艺", "CS-建议型（待人审核定）"
    return "机械粉碎 + 分级除尘配套", "通用粉碎主机（待人审核定）"


def _motor_kw_by_capacity(tph: float | None) -> str:
    if tph is None:
        return "待核定（按产量与物料特性）"
    if tph < 0.5:
        return "15~22 kW（初估）"
    if tph < 2:
        return "37~55 kW（初估）"
    if tph < 5:
        return "75~110 kW（初估）"
    return "132 kW 及以上（初估，须人审）"


def _parse_feed_mm(text: str | None) -> float | None:
    """从进料尺寸文本解析毫米数（取首个数字）。"""
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|毫米)?", text, re.I)
    return float(m.group(1)) if m else None


def _needs_pre_crusher(slots: InquirySlots) -> bool:
    """
    入料偏粗或中等粒度时默认加预破碎（偏保守，便于人审纠偏沉淀经验）。

    例：6mm 仍可能被加上 → 人审说明「已达磨机入料要求」→ 经验下次自动去掉。
    """
    feed = slots.feed_size or ""
    if any(k in feed for k in ("块", "大块", "原矿", "破碎前")):
        return True
    mm = _parse_feed_mm(feed)
    if mm is not None and mm > 5:
        return True
    return False


def build_configuration(
    slots: InquirySlots,
    *,
    kb_snippets: list[dict[str, Any]] | None = None,
) -> ConfigurationProposal:
    """规则生成配置清单 + 设备参数。"""
    route, host_model = _suggest_host(slots)
    tph = _parse_capacity_tph(slots.capacity)
    sets = float(slots.equipment_qty) if slots.equipment_qty and slots.equipment_qty > 0 else 1.0
    motor = _motor_kw_by_capacity(tph)
    need_crusher = _needs_pre_crusher(slots)

    items: list[ConfigLineItem] = []
    no = 1
    if need_crusher:
        items.append(
            ConfigLineItem(
                item_no=no,
                category="破碎",
                name="预破碎机",
                model_spec="颚式/锤式预破（按进料尺寸选型）",
                quantity=sets,
                unit="套",
                remark=f"进料 {slots.feed_size or '—'} 偏粗，系统默认加预破；若已达磨机入料可人审去掉",
            )
        )
        no += 1

    items.extend(
        [
            ConfigLineItem(
                item_no=no,
                category="主机",
                name="粉碎主机",
                model_spec=host_model,
                quantity=sets,
                unit="套",
                remark=f"工艺路线：{route}",
            ),
            ConfigLineItem(
                item_no=no + 1,
                category="分级",
                name="分级机 / 分级轮系统",
                model_spec="与主机匹配的分级装置",
                quantity=sets,
                unit="套",
                remark=f"目标细度 {slots.fineness or '—'}；通筛率 {slots.sieve_pass_rate or '—'}",
            ),
            ConfigLineItem(
                item_no=no + 2,
                category="给料",
                name="定量给料机",
                model_spec="螺旋/振动给料（按物料流动性选型）",
                quantity=sets,
                unit="套",
                remark=f"适配进料尺寸 {slots.feed_size or '—'}",
            ),
            ConfigLineItem(
                item_no=no + 3,
                category="除尘",
                name="脉冲袋式除尘器",
                model_spec="按风量配套",
                quantity=sets,
                unit="套",
                remark="含卸灰阀；滤材按物料腐蚀/温度另定",
            ),
            ConfigLineItem(
                item_no=no + 4,
                category="风机",
                name="引风机",
                model_spec="与除尘风量匹配",
                quantity=sets,
                unit="台",
                remark="",
            ),
            ConfigLineItem(
                item_no=no + 5,
                category="管道附件",
                name="进料/出料管道、弯头、软连接",
                model_spec="按现场布置",
                quantity=1,
                unit="批",
                remark="含必要阀门与支架",
            ),
            ConfigLineItem(
                item_no=no + 6,
                category="电气",
                name="电控柜及变频控制",
                model_spec=slots.power_supply or "380V/50Hz（默认假设）",
                quantity=sets,
                unit="套",
                remark="主机/分级/风机联锁；急停与接地按规范",
            ),
            ConfigLineItem(
                item_no=no + 7,
                category="辅助",
                name="空压机及气路（若脉冲喷吹需要）",
                model_spec="按阀数量估算排气量",
                quantity=1,
                unit="套",
                remark="无气源现场需单列",
            ),
        ]
    )
    # 重新编号，保证连续
    for i, it in enumerate(items, start=1):
        it.item_no = i

    params = EquipmentParams(
        host_model=host_model,
        params={
            "工艺路线": route,
            "加工物料": slots.material or "",
            "成品细度目标（目/D50，非 d95）": slots.fineness or "",
            "通筛率目标（含 d95/D95）": slots.sieve_pass_rate or "",
            "设计产量": slots.capacity or "",
            "进料尺寸": slots.feed_size or "",
            "是否预破碎（规则初判）": "是" if need_crusher else "否",
            "主电机功率（初估）": motor,
            "主机套数": str(int(sets) if sets == int(sets) else sets),
            "分级转速": "待人审按细度曲线核定",
            "系统风量": "待人审按产量与除尘负荷核定",
            "物料含水": slots.moisture or "未提供",
            "物料特性": slots.hardness or "未提供",
            "电源": slots.power_supply or "未提供（按 380V/50Hz 假设）",
        },
        notes=slots.notes or "",
    )

    warnings: list[str] = []
    if need_crusher:
        warnings.append(
            "进料偏粗：已默认配置预破碎机；若入料已达磨机要求，请在人审中说明以便沉淀经验。"
        )
    if not slots.moisture:
        warnings.append("未提供含水率：潮湿物料可能需烘干或改给料形式。")
    if not slots.hardness:
        warnings.append("未提供硬度/磨蚀性：易磨件材质与寿命需人审确认。")
    if tph is None:
        warnings.append("产量未能解析为数值：电机与风机功率仅为占位，须人审核定。")
    warnings.append(
        "术语：D95/d95 表示通筛率（通过筛网的质量占比），不是细度；"
        "细度请用目数或 D50 等粒度指标。"
    )
    warnings.append("型号与功率为规则初估，正式配置以人工审核修订版为准。")

    overview = (
        f"依据工艺条件（{slots.process_summary()}），建议采用「{route}」。"
        f"以下给出主机及配套的配置清单与关键设备参数初稿，供选型与商务确认。"
    )

    return ConfigurationProposal(
        title=f"{slots.material or '物料'} · 工艺选型配置方案",
        overview=overview,
        line_items=items,
        equipment_params=params,
        process_basis=slots.process_summary(),
        kb_snippets=list(kb_snippets or []),
        warnings=warnings,
    )


def format_proposal_text(proposal: ConfigurationProposal) -> str:
    """把配置方案格式化为客服可直接展示的正文。"""
    lines: list[str] = [
        f"【{proposal.title}】",
        "",
        proposal.overview or "",
        "",
        "一、配置清单",
    ]
    for it in proposal.line_items:
        lines.append(
            f"{it.item_no}. [{it.category}] {it.name}｜{it.model_spec} "
            f"× {it.quantity}{it.unit}"
        )
        if it.remark:
            lines.append(f"   备注：{it.remark}")
    ep = proposal.equipment_params
    lines.extend(["", "二、设备配置参数", f"主机型号建议：{ep.host_model or '—'}"])
    for k, v in (ep.params or {}).items():
        if v:
            lines.append(f"- {k}：{v}")
    if ep.notes:
        lines.append(f"- 其他说明：{ep.notes}")
    if proposal.process_basis:
        lines.extend(["", f"工艺依据：{proposal.process_basis}"])
    if proposal.warnings:
        lines.extend(["", "三、注意事项"])
        for w in proposal.warnings:
            lines.append(f"- {w}")
    if proposal.caution:
        lines.extend(["", proposal.caution])
    return "\n".join(lines).strip()


def enrich_overview_with_llm(
    proposal: ConfigurationProposal,
    *,
    project_id: str = "",
    model: str | None = None,
) -> ConfigurationProposal:
    """可选：经中台 MCP 润色 overview，不改动清单结构。"""
    if not (project_id or "").strip():
        return proposal
    try:
        from mcp_llm import chat_via_mcp

        prompt = (
            "你是粉体设备选型工程师。根据工艺依据与配置清单，用中文写一段不超过200字的方案概述。"
            "不要编造清单中没有的设备，不要给出具体成交价格。\n"
            f"工艺依据: {proposal.process_basis}\n"
            f"现有概述: {proposal.overview}\n"
            f"主机: {proposal.equipment_params.host_model}\n"
        )
        text = chat_via_mcp(
            project_id=project_id,
            prompt=prompt,
            model=(model or "").strip() or None,
        ).strip()
        if text:
            proposal.overview = text[:500]
    except Exception as exc:
        logger.warning("LLM 润色 overview 失败: %s", exc)
    return proposal
