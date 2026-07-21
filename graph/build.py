"""编译配置选型 LangGraph。"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from harness import QuotationHarness
from store import QuotationStore

from graph.nodes import NodeContext, make_nodes
from graph.state import QuotationState

logger = logging.getLogger(__name__)

_CHECKPOINTER = None
_POOL = None


def _make_checkpointer():
    global _CHECKPOINTER, _POOL
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    from config import settings

    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        from db import build_dsn

        schema = settings.checkpoint_schema
        with psycopg.connect(build_dsn()) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            conn.commit()

        dsn = build_dsn()
        if "?" in dsn:
            dsn_opts = f"{dsn}&options=-csearch_path%3D{schema},public"
        else:
            dsn_opts = f"{dsn}?options=-csearch_path%3D{schema},public"

        _POOL = ConnectionPool(
            conninfo=dsn_opts,
            max_size=max(2, settings.db_pool_max_size),
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=True,
        )
        checkpointer = PostgresSaver(_POOL)
        checkpointer.setup()
        _CHECKPOINTER = checkpointer
        logger.info("LangGraph checkpoint schema=%s", schema)
        return checkpointer
    except Exception as exc:
        if not settings.allow_memory_checkpoint:
            raise RuntimeError(f"Postgres checkpoint 初始化失败: {exc}") from exc
        logger.warning("回退 MemorySaver: %s", exc)
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
        return _CHECKPOINTER


def build_graph(
    store: QuotationStore,
    harness: QuotationHarness,
    *,
    extract_fn,
    search_fn=None,
    experience=None,
    project_id: str = "",
    llm_api_key: str = "",
    llm_model: str | None = None,
):
    ctx = NodeContext(
        store,
        harness,
        extract_fn=extract_fn,
        search_fn=search_fn,
        experience=experience,
        project_id=project_id or "",
        llm_api_key=llm_api_key,
        llm_model=(llm_model or "").strip() or None,
    )
    nodes = make_nodes(ctx)

    g = StateGraph(QuotationState)
    g.add_node("parse_slots", nodes["parse_slots"])
    g.add_node("clarify", nodes["clarify"])
    g.add_node("generate_config", nodes["generate_config"])
    g.add_node("human_review", nodes["human_review"])
    g.add_node("finalize", nodes["finalize"])
    g.add_node("end_fail", nodes["end_fail"])

    g.add_edge(START, "parse_slots")
    g.add_conditional_edges(
        "parse_slots",
        nodes["route_completeness"],
        {
            "clarify": "clarify",
            "generate_config": "generate_config",
            "end_fail": "end_fail",
        },
    )
    g.add_conditional_edges(
        "clarify",
        nodes["route_after_clarify"],
        {"parse_slots": "parse_slots", "end_fail": "end_fail"},
    )
    g.add_edge("generate_config", "human_review")
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    g.add_edge("end_fail", END)

    return g.compile(checkpointer=_make_checkpointer())
