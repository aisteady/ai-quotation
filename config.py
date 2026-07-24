"""
AI 询报价系统 — 配置加载（config.py）
====================================

学习要点
--------
1. 上层应用不硬编码密钥，全部走环境变量 / `.env`。
2. **加载顺序**：本目录 `.env` 先加载；仓库根 `.env` 后加载且
   `override=False`，所以本应用自己的配置优先，缺的项再从中台根继承
   （例如 DB_HOST、DB_PASSWORD）。
3. `Settings` 用 `@dataclass(frozen=True)`：启动时读一次，运行期不可改，
   避免「改了环境变量但进程里还是旧值」的困惑。
4. 业务表落在中台项目 Schema（默认 `ai_inquiry_quotation`），与
   `PROJECT_ID` 对应；MCP 检索也用同一个 `project_id`。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 本文件所在目录 = models/ai_quotation/
_APP_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _repo_root_env() -> Path | None:
    """
    向上找含 `pyproject.toml` 的仓库根，再取其 `.env`。

    为什么要向上找？因为询报价可能在 monorepo 子目录运行，
    数据库账号往往只配在中台根 `.env` 里。
    """
    for parent in _APP_DIR.parents:
        if (parent / "pyproject.toml").exists():
            env_path = parent / ".env"
            return env_path if env_path.exists() else None
    return None


def _load_env() -> None:
    """先本地后根目录；根目录不覆盖本地已有键。"""
    load_dotenv(_APP_DIR / ".env")
    root_env = _repo_root_env()
    if root_env is not None:
        load_dotenv(root_env, override=False)


# 模块 import 时立刻加载，保证后面 Settings 字段默认值能读到环境变量
_load_env()


def env(name: str, default: str = "") -> str:
    """读字符串环境变量并 strip，避免尾部空格导致鉴权失败。"""
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    """把 1/true/yes/on 解析为 True。"""
    raw = env(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """
    全局配置快照。

    字段分组：
    - 运行环境 / Streamlit 端口
    - 中台 REST + MCP（检索价目知识）
    - LLM（可选，用于更准的槽位抽取）
    - Loop / Harness 参数
    - PostgreSQL（业务表 + LangGraph checkpoint）
    """

    # ---- 环境 ----
    environment: str = env("ENVIRONMENT", "development")

    # ---- UI ----
    # 8504 避开客服 8502、合同 8503、中台 UI 8501
    port: int = int(env("PORT", env("STREAMLIT_SERVER_PORT", "8504")) or "8504")
    server_address: str = env("STREAMLIT_SERVER_ADDRESS", "0.0.0.0")

    # ---- 中台 REST（Bearer 可用 SERVICE_TOKEN 兼容多种命名）----
    api_base_url: str = env("API_BASE_URL", "http://127.0.0.1:8000")
    api_bearer_token: str = (
        env("API_BEARER_TOKEN")
        or env("SERVICE_TOKEN")
        or env("MCP_API_KEY")
        or env("MCP_SERVICE_TOKEN")
    )

    # ---- MCP：默认官方 Streamable HTTP ----
    # MCP_CLIENT_TOKEN 必须与中台网关密钥一致；勿用项目 SERVICE_TOKEN（那是 REST JWT）
    mcp_transport: str = env("MCP_TRANSPORT", "http")
    mcp_url: str = env("MCP_URL", "http://127.0.0.1:8765/mcp")
    mcp_client_token: str = env("MCP_CLIENT_TOKEN")
    # TCP 遗留模式（一般不用）
    mcp_host: str = env("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(env("MCP_PORT", "8766") or "8766")
    mcp_tcp_secret: str = env("MCP_TCP_SECRET")
    mcp_timeout: float = float(env("MCP_TIMEOUT", "120") or "120")

    # ---- 中台项目 & LLM ----
    project_id: str = env("PROJECT_ID")
    # 空则不传 model，使用中台「大模型管理」项目配置
    llm_model: str = env("LLM_MODEL")
    top_k: int = int(env("TOP_K", "5") or "5")
    search_threshold: float = float(env("SEARCH_THRESHOLD", "0.45") or "0.45")

    # ---- 产品内 Loop / Harness ----
    # 信息不足时最多追问几轮（图内 clarify → parse 循环）
    max_clarify_loops: int = int(env("MAX_CLARIFY_LOOPS", "5") or "5")
    # 后台定时扫「待人审」；0 = 不启线程（开发可关）
    harness_loop_interval_sec: int = int(env("HARNESS_LOOP_INTERVAL_SEC", "0") or "0")
    # 配置经验召回阈值（Jaccard + 槽位提示）
    experience_apply_threshold: float = float(
        env("EXPERIENCE_APPLY_THRESHOLD", "0.35") or "0.35"
    )

    # ---- 对智能客服开放的同步推荐 HTTP ----
    customer_api_host: str = env("CUSTOMER_API_HOST", "0.0.0.0")
    customer_api_port: int = int(env("CUSTOMER_API_PORT", "8510") or "8510")
    # 空 = 开发可不鉴权；生产建议配置
    customer_api_token: str = env("CUSTOMER_API_TOKEN")

    # ---- 数据库 ----
    database_url: str = env("DATABASE_URL")
    db_host: str = env("DB_HOST", "localhost")
    db_port: int = int(env("DB_PORT", "5432") or "5432")
    db_user: str = env("DB_USER", "postgres")
    db_password: str = env("DB_PASSWORD", "password")
    db_name: str = env("DB_NAME", "aibase")
    db_sslmode: str = env("DB_SSLMODE", "")
    db_connect_timeout: int = int(env("DB_CONNECT_TIMEOUT", "10") or "10")
    # 业务表 schema：与中台「项目管理」里创建的 Schema 对齐
    db_schema: str = env("APP_DB_SCHEMA", "ai_inquiry_quotation")
    # LangGraph 断点（interrupt 后人审回来要靠它恢复）
    checkpoint_schema: str = env("CHECKPOINT_SCHEMA", "") or env(
        "APP_DB_SCHEMA", "ai_inquiry_quotation"
    )
    db_pool_max_size: int = int(env("DB_POOL_MAX_SIZE", "10") or "10")
    # 生产禁止静默降级到 MemorySaver（重启会丢人审状态）
    allow_memory_checkpoint: bool = env_bool(
        "ALLOW_MEMORY_CHECKPOINT",
        default=env("ENVIRONMENT", "development") != "production",
    )

    app_dir: Path = _APP_DIR

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


# 单例：全项目 `from config import settings`
settings = Settings()


def validate_settings(*, strict: bool | None = None) -> list[str]:
    """
    启动时自检。

    - strict=None：生产环境按严格模式，开发环境只 warning
    - fatal 项会抛 RuntimeError，阻止带着错误配置上线
    """
    warnings: list[str] = []
    fatal: list[str] = []
    strict = settings.is_production if strict is None else strict

    if not settings.project_id:
        (fatal if strict else warnings).append("未配置 PROJECT_ID")
    if not settings.database_url and settings.db_password in ("", "password"):
        (fatal if strict else warnings).append(
            "数据库密码疑似未配置（DB_PASSWORD/DATABASE_URL）"
        )
    if settings.max_clarify_loops < 1:
        fatal.append("MAX_CLARIFY_LOOPS 必须 >= 1")

    for w in warnings:
        logger.warning("config: %s", w)
    if fatal:
        raise RuntimeError("配置校验失败: " + "; ".join(fatal))
    return warnings
