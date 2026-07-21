"""
工艺选型配置 — Streamlit UI
主目标：输出详细配置清单 + 设备参数（人审确认）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import streamlit as st

from schemas import InquiryStatus
from service import QuotationService

st.set_page_config(page_title="AI 工艺选型配置", layout="wide")
st.title("AI 工艺选型配置系统")
st.caption(
    "工艺五要素 → 配置清单 → 人工审核（可写修订意见）→ 沉淀选型经验并在后续单自动套用"
)


@st.cache_resource
def get_service() -> QuotationService:
    return QuotationService(start_harness_loop=True)


svc = get_service()

if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "last" not in st.session_state:
    st.session_state.last = None

tab_ask, tab_human, tab_hist, tab_exp = st.tabs(
    ["需求对话", "配置审核", "历史方案", "选型经验"]
)

with tab_ask:
    st.subheader("自然语言需求")
    st.caption(
        "必填：加工物料、成品细度、通筛率、产量、进料尺寸。系统据此生成配置清单与设备参数。"
    )
    text = st.text_area(
        "描述",
        placeholder=(
            "例：物料石英砂，成品细度 200 目，通筛率 ≥95%，"
            "产量 2t/h，进料尺寸 <30mm，含水约 1%，希望气流粉碎工艺"
        ),
        height=120,
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("提交需求", type="primary"):
            if not text.strip():
                st.warning("请输入需求")
            else:
                try:
                    st.session_state.last = svc.start_inquiry(text.strip())
                    st.session_state.thread_id = st.session_state.last.get("thread_id")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
    with c2:
        if st.button("清空会话"):
            st.session_state.thread_id = None
            st.session_state.last = None
            st.rerun()

    last = st.session_state.last
    if last:
        st.write("状态:", last.get("status"), "· thread:", last.get("thread_id"))
        payload = last.get("interrupt_payload")
        if last.get("interrupted") and payload:
            if payload.get("type") == "clarify":
                st.info(payload.get("question") or "请补充信息")
                reply = st.text_input("补充回复", key="clarify_reply")
                if st.button("提交补充"):
                    try:
                        st.session_state.last = svc.resume_clarify(
                            st.session_state.thread_id, reply.strip()
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            elif payload.get("type") == "human_config":
                st.success("已生成配置初稿，请到「配置审核」页确认或修订。")
                cfg = payload.get("draft_config") or {}
                if cfg.get("overview"):
                    st.write(cfg["overview"])
                if cfg.get("warnings"):
                    for w in cfg["warnings"]:
                        st.warning(w)
        elif last.get("status") == InquiryStatus.CONFIGURED.value:
            st.success(f"配置已确认：{last.get('configuration_id')}")
        elif last.get("error"):
            st.error(last.get("error"))

with tab_human:
    st.subheader("待审核配置方案")
    pending = svc.store.list_pending_human(limit=30)
    if not pending:
        st.info("暂无待审方案")
    for rec in pending:
        title = (rec.draft_config.title if rec.draft_config else None) or (
            rec.slots.material or "未命名"
        )
        with st.expander(f"{title} [{rec.id[:8]}…]"):
            st.write("工艺依据:", rec.slots.process_summary())
            st.write("原文:", rec.raw_text)
            draft = rec.draft_config
            if not draft:
                st.error("无 draft_config")
                continue
            st.markdown("#### 方案概述")
            st.write(draft.overview)
            if draft.warnings:
                for w in draft.warnings:
                    st.warning(w)

            st.markdown("#### 配置清单")
            st.dataframe(
                [
                    {
                        "序号": i.item_no,
                        "类别": i.category,
                        "名称": i.name,
                        "型号/规格": i.model_spec,
                        "数量": i.quantity,
                        "单位": i.unit,
                        "备注": i.remark,
                    }
                    for i in draft.line_items
                ],
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("#### 设备配置参数")
            st.write("主机型号建议:", draft.equipment_params.host_model)
            st.json(draft.equipment_params.params)
            if draft.kb_snippets:
                st.markdown("#### 知识库参考")
                st.write(draft.kb_snippets[:2])

            rationale = st.text_area(
                "审核说明",
                key=f"rat_{rec.id}",
                placeholder="例如：可以 / 同意按此清单执行",
            )
            # 自然语言修订意见（常见）；完整 JSON 仅高级用户使用
            revision = st.text_area(
                "修订意见（自然语言，可选）",
                value="",
                key=f"rev_{rec.id}",
                height=80,
                placeholder=(
                    "例：入料 6mm 已达磨机所需入料细度，不用加破碎机。"
                    "有实质修订时系统会归纳为经验，供后续同类单自动套用。"
                ),
            )
            with st.expander("高级：用完整 JSON 覆盖配置初稿"):
                edit_json = st.text_area(
                    "ConfigurationProposal JSON",
                    value="",
                    key=f"json_{rec.id}",
                    height=100,
                    placeholder="仅当需要整份替换时粘贴；普通修订请用上方「修订意见」",
                )
            b1, b2 = st.columns(2)
            with b1:
                if st.button("确认配置方案", key=f"ok_{rec.id}", type="primary"):
                    try:
                        # 合并审核说明 + 自然语言修订
                        notes = (rationale or "").strip()
                        rev = (revision or "").strip()
                        if rev:
                            notes = f"{notes}\n【修订意见】{rev}".strip() if notes else f"【修订意见】{rev}"

                        payload: dict = {
                            "reject": False,
                            "rationale": notes,
                        }

                        raw_json = (edit_json or "").strip()
                        if raw_json:
                            # 仅当内容像 JSON 对象时才解析，避免把中文意见当成 JSON
                            if raw_json.startswith("{"):
                                try:
                                    payload["proposal"] = json.loads(raw_json)
                                except json.JSONDecodeError as je:
                                    st.error(
                                        f"高级 JSON 格式无效：{je}。"
                                        "若只是文字修正，请填在「修订意见」里，不要放在 JSON 框。"
                                    )
                                    st.stop()
                            else:
                                st.error(
                                    "「高级 JSON」须以 { 开头。"
                                    "文字修正请写在「修订意见（自然语言）」中。"
                                )
                                st.stop()

                        # 把修订意见写入方案 notes，方便落库后仍能看到
                        if rev and "proposal" not in payload:
                            from copy import deepcopy

                            p = deepcopy(draft)
                            ep_notes = (p.equipment_params.notes or "").strip()
                            add = f"人审修订：{rev}"
                            p.equipment_params.notes = (
                                f"{ep_notes}\n{add}".strip() if ep_notes else add
                            )
                            if "d95" in rev.lower() or "D95" in rev or "通筛" in rev:
                                p.warnings = list(p.warnings or [])
                                tip = (
                                    "术语提醒：D95/d95 一般表示通筛率（通过某筛网的质量占比），"
                                    "不是细度；细度请用目数或 D50 等粒度指标表述。"
                                )
                                if tip not in p.warnings:
                                    p.warnings.append(tip)
                            payload["proposal"] = p.model_dump(mode="json")

                        st.session_state.last = svc.resume_human_config(
                            rec.thread_id, payload
                        )
                        learned = (st.session_state.last or {}).get(
                            "learned_experiences"
                        ) or []
                        if learned:
                            st.success(
                                f"配置已确认并落库；已沉淀 {len(learned)} 条选型经验"
                            )
                        else:
                            st.success("配置已确认并落库")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            with b2:
                if st.button("拒绝方案", key=f"no_{rec.id}"):
                    try:
                        svc.resume_human_config(
                            rec.thread_id,
                            {
                                "reject": True,
                                "reject_reason": rationale or revision or "人审拒绝",
                            },
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

with tab_hist:
    st.subheader("历史需求 / 配置")
    rows = svc.store.list_inquiries(limit=40)
    if not rows:
        st.info("暂无记录")
    else:
        st.dataframe(
            [
                {
                    "id": r.id[:8],
                    "status": r.status.value,
                    "物料": r.slots.material,
                    "细度": r.slots.fineness,
                    "产量": r.slots.capacity,
                    "configuration": (r.configuration_id or "")[:8],
                    "updated": r.updated_at,
                }
                for r in rows
            ],
            use_container_width=True,
            hide_index=True,
        )
        pick = st.selectbox(
            "查看已确认配置",
            options=["—"]
            + [r.configuration_id for r in rows if r.configuration_id],
        )
        if pick and pick != "—":
            cfg = svc.store.get_configuration(pick)
            if cfg:
                st.write(cfg.title)
                st.write(cfg.overview)
                st.json(json.loads(cfg.line_items_json))
                st.json(json.loads(cfg.equipment_params_json))

with tab_exp:
    st.subheader("选型经验库")
    st.caption(
        "来自人审修订意见的归纳结果。生成新方案时会按相似度自动套用"
        "（例如：细入料不加破碎机）。"
    )
    exps = svc.store.list_experiences(limit=80)
    if not exps:
        st.info("暂无经验。在「配置审核」填写修订意见并确认后，将自动沉淀。")
    else:
        st.dataframe(
            [
                {
                    "标题": e.title,
                    "动作": e.action_type.value,
                    "目标": e.action_target,
                    "规则": e.rule_text[:60],
                    "命中": e.hit_count,
                    "有效": e.is_active,
                    "更新": e.updated_at,
                }
                for e in exps
            ],
            use_container_width=True,
            hide_index=True,
        )
        pick_e = st.selectbox(
            "查看详情",
            options=["—"] + [f"{e.title} ({e.id[:8]})" for e in exps],
        )
        if pick_e and pick_e != "—":
            eid = pick_e.rsplit("(", 1)[-1].rstrip(")")
            matched = next((e for e in exps if e.id.startswith(eid)), None)
            if matched:
                st.json(matched.model_dump(mode="json"))
                if matched.is_active and st.button("停用该经验", key=f"off_{matched.id}"):
                    svc.store.deactivate_experience(matched.id)
                    st.rerun()
