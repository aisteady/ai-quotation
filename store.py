"""
持久化 — 需求单 / 配置方案 / Harness 事件
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from config import settings
from db import connect
from schemas import (
    ConfigExperienceRecord,
    ConfigurationProposal,
    ConfigurationRecord,
    InquiryRecord,
    InquirySlots,
    InquiryStatus,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class QuotationStore:
    """命名保留兼容；实际存「需求 + 配置」。"""

    def __init__(self) -> None:
        self.schema = settings.db_schema
        self._ensure()

    def _q(self, table: str) -> str:
        return f'"{self.schema}"."{table}"'

    def _ensure(self) -> None:
        with connect() as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("inquiries")} (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    raw_text TEXT NOT NULL DEFAULT '',
                    slots_json TEXT NOT NULL DEFAULT '{{}}',
                    clarify_round INT NOT NULL DEFAULT 0,
                    draft_config_json TEXT,
                    configuration_id TEXT,
                    trace_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # 兼容旧列名：若存在 draft_hint_json / quotation_id 不强制迁移
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("messages")} (
                    id TEXT PRIMARY KEY,
                    inquiry_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("configurations")} (
                    id TEXT PRIMARY KEY,
                    inquiry_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    overview TEXT NOT NULL DEFAULT '',
                    line_items_json TEXT NOT NULL DEFAULT '[]',
                    equipment_params_json TEXT NOT NULL DEFAULT '{{}}',
                    process_basis TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("harness_events")} (
                    id TEXT PRIMARY KEY,
                    inquiry_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("config_experiences")} (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    rule_text TEXT NOT NULL DEFAULT '',
                    condition_summary TEXT NOT NULL DEFAULT '',
                    condition_keywords_json TEXT NOT NULL DEFAULT '[]',
                    slot_hints_json TEXT NOT NULL DEFAULT '{{}}',
                    action_type TEXT NOT NULL DEFAULT 'note',
                    action_target TEXT NOT NULL DEFAULT '',
                    action_payload_json TEXT NOT NULL DEFAULT '{{}}',
                    feedback_raw TEXT NOT NULL DEFAULT '',
                    source_inquiry_id TEXT,
                    version INT NOT NULL DEFAULT 1,
                    parent_experience_id TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    hit_count INT NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_config_exp_active
                ON {self._q("config_experiences")} (is_active)
                """
            )
            # 旧表可能缺新列：尝试 ADD COLUMN（已存在则忽略）
            for ddl in (
                f'ALTER TABLE {self._q("inquiries")} ADD COLUMN IF NOT EXISTS draft_config_json TEXT',
                f'ALTER TABLE {self._q("inquiries")} ADD COLUMN IF NOT EXISTS configuration_id TEXT',
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass
            conn.commit()

    def upsert_inquiry(self, rec: InquiryRecord) -> None:
        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("inquiries")} (
                    id, thread_id, status, raw_text, slots_json, clarify_round,
                    draft_config_json, configuration_id, trace_id, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    thread_id = EXCLUDED.thread_id,
                    status = EXCLUDED.status,
                    raw_text = EXCLUDED.raw_text,
                    slots_json = EXCLUDED.slots_json,
                    clarify_round = EXCLUDED.clarify_round,
                    draft_config_json = EXCLUDED.draft_config_json,
                    configuration_id = EXCLUDED.configuration_id,
                    trace_id = EXCLUDED.trace_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    rec.id,
                    rec.thread_id,
                    rec.status.value,
                    rec.raw_text,
                    rec.slots.model_dump_json(),
                    rec.clarify_round,
                    rec.draft_config.model_dump_json() if rec.draft_config else None,
                    rec.configuration_id,
                    rec.trace_id,
                    rec.created_at or _utc_now(),
                    rec.updated_at or _utc_now(),
                ),
            )
            conn.commit()

    def get_inquiry(self, inquiry_id: str) -> InquiryRecord | None:
        with connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {self._q('inquiries')} WHERE id = %s",
                (inquiry_id,),
            ).fetchone()
        return self._row_inquiry(row) if row else None

    def list_inquiries(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[InquiryRecord]:
        sql = f"SELECT * FROM {self._q('inquiries')}"
        params: list[Any] = []
        if status:
            sql += " WHERE status = %s"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_inquiry(r) for r in rows]

    def list_pending_human(self, limit: int = 50) -> list[InquiryRecord]:
        return self.list_inquiries(status=InquiryStatus.PENDING_HUMAN.value, limit=limit)

    def add_message(self, inquiry_id: str, role: str, content: str) -> None:
        import uuid

        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("messages")} (id, inquiry_id, role, content, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), inquiry_id, role, content, _utc_now()),
            )
            conn.commit()

    def save_configuration(self, c: ConfigurationRecord) -> None:
        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("configurations")} (
                    id, inquiry_id, title, overview, line_items_json,
                    equipment_params_json, process_basis, rationale,
                    created_by, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    c.id,
                    c.inquiry_id,
                    c.title,
                    c.overview,
                    c.line_items_json,
                    c.equipment_params_json,
                    c.process_basis,
                    c.rationale,
                    c.created_by,
                    c.created_at or _utc_now(),
                ),
            )
            conn.commit()

    def get_configuration(self, configuration_id: str) -> ConfigurationRecord | None:
        with connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {self._q('configurations')} WHERE id = %s",
                (configuration_id,),
            ).fetchone()
        if not row:
            return None
        return ConfigurationRecord(
            id=row["id"],
            inquiry_id=row["inquiry_id"],
            title=row.get("title") or "",
            overview=row.get("overview") or "",
            line_items_json=row.get("line_items_json") or "[]",
            equipment_params_json=row.get("equipment_params_json") or "{}",
            process_basis=row.get("process_basis") or "",
            rationale=row.get("rationale") or "",
            created_by=row.get("created_by") or "human",
            created_at=row.get("created_at") or "",
        )

    def log_harness_event(
        self,
        event_type: str,
        *,
        inquiry_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        import uuid

        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("harness_events")}
                (id, inquiry_id, event_type, payload_json, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    inquiry_id,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            conn.commit()

    def upsert_experience(self, exp: ConfigExperienceRecord) -> None:
        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("config_experiences")} (
                    id, title, rule_text, condition_summary,
                    condition_keywords_json, slot_hints_json,
                    action_type, action_target, action_payload_json,
                    feedback_raw, source_inquiry_id, version,
                    parent_experience_id, is_active, hit_count,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    rule_text = EXCLUDED.rule_text,
                    condition_summary = EXCLUDED.condition_summary,
                    condition_keywords_json = EXCLUDED.condition_keywords_json,
                    slot_hints_json = EXCLUDED.slot_hints_json,
                    action_type = EXCLUDED.action_type,
                    action_target = EXCLUDED.action_target,
                    action_payload_json = EXCLUDED.action_payload_json,
                    feedback_raw = EXCLUDED.feedback_raw,
                    source_inquiry_id = EXCLUDED.source_inquiry_id,
                    version = EXCLUDED.version,
                    parent_experience_id = EXCLUDED.parent_experience_id,
                    is_active = EXCLUDED.is_active,
                    hit_count = EXCLUDED.hit_count,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    exp.id,
                    exp.title,
                    exp.rule_text,
                    exp.condition_summary,
                    json.dumps(exp.condition_keywords, ensure_ascii=False),
                    json.dumps(exp.slot_hints, ensure_ascii=False),
                    exp.action_type.value,
                    exp.action_target,
                    json.dumps(exp.action_payload, ensure_ascii=False),
                    exp.feedback_raw,
                    exp.source_inquiry_id,
                    exp.version,
                    exp.parent_experience_id,
                    exp.is_active,
                    exp.hit_count,
                    exp.created_at or _utc_now(),
                    exp.updated_at or _utc_now(),
                ),
            )
            conn.commit()

    def list_active_experiences(self, limit: int = 200) -> list[ConfigExperienceRecord]:
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {self._q("config_experiences")}
                WHERE is_active = TRUE
                ORDER BY hit_count DESC, updated_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [self._row_experience(r) for r in rows]

    def list_experiences(self, *, limit: int = 50, active_only: bool = False) -> list[ConfigExperienceRecord]:
        sql = f"SELECT * FROM {self._q('config_experiences')}"
        if active_only:
            sql += " WHERE is_active = TRUE"
        sql += " ORDER BY updated_at DESC LIMIT %s"
        with connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_experience(r) for r in rows]

    def find_similar_experience(
        self,
        *,
        title: str,
        action_type: str,
        action_target: str,
    ) -> ConfigExperienceRecord | None:
        with connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {self._q("config_experiences")}
                WHERE is_active = TRUE
                  AND action_type = %s
                  AND action_target = %s
                  AND title = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (action_type, action_target, title),
            ).fetchone()
        return self._row_experience(row) if row else None

    def bump_experience_hit(self, experience_id: str) -> None:
        with connect() as conn:
            conn.execute(
                f"""
                UPDATE {self._q("config_experiences")}
                SET hit_count = hit_count + 1, updated_at = %s
                WHERE id = %s
                """,
                (_utc_now(), experience_id),
            )
            conn.commit()

    def deactivate_experience(self, experience_id: str) -> None:
        with connect() as conn:
            conn.execute(
                f"""
                UPDATE {self._q("config_experiences")}
                SET is_active = FALSE, updated_at = %s
                WHERE id = %s
                """,
                (_utc_now(), experience_id),
            )
            conn.commit()

    def _row_experience(self, row: dict) -> ConfigExperienceRecord:
        from schemas import ConfigActionType

        try:
            action = ConfigActionType(row.get("action_type") or "note")
        except ValueError:
            action = ConfigActionType.NOTE
        return ConfigExperienceRecord(
            id=row["id"],
            title=row.get("title") or "",
            rule_text=row.get("rule_text") or "",
            condition_summary=row.get("condition_summary") or "",
            condition_keywords=json.loads(row.get("condition_keywords_json") or "[]"),
            slot_hints=json.loads(row.get("slot_hints_json") or "{}"),
            action_type=action,
            action_target=row.get("action_target") or "",
            action_payload=json.loads(row.get("action_payload_json") or "{}"),
            feedback_raw=row.get("feedback_raw") or "",
            source_inquiry_id=row.get("source_inquiry_id"),
            version=int(row.get("version") or 1),
            parent_experience_id=row.get("parent_experience_id"),
            is_active=bool(row.get("is_active", True)),
            hit_count=int(row.get("hit_count") or 0),
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
        )

    def _row_inquiry(self, row: dict) -> InquiryRecord:
        draft = None
        raw_draft = row.get("draft_config_json") or row.get("draft_hint_json")
        if raw_draft:
            try:
                draft = ConfigurationProposal.model_validate_json(raw_draft)
            except Exception:
                draft = None
        cfg_id = row.get("configuration_id") or row.get("quotation_id")
        status_raw = row.get("status") or "draft"
        if status_raw == "quoted":
            status_raw = InquiryStatus.CONFIGURED.value
        return InquiryRecord(
            id=row["id"],
            thread_id=row["thread_id"],
            status=InquiryStatus(status_raw),
            raw_text=row.get("raw_text") or "",
            slots=InquirySlots.model_validate_json(row.get("slots_json") or "{}"),
            clarify_round=int(row.get("clarify_round") or 0),
            draft_config=draft,
            configuration_id=cfg_id,
            trace_id=row.get("trace_id"),
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
        )
