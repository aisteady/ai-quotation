"""
PostgreSQL 连接（db.py）
=======================

学习要点
--------
1. 业务代码统一 `connect()`，不要散落拼连接串。
2. 优先 `DATABASE_URL`（云托管常见）；否则用 DB_HOST/USER/PASSWORD 拼。
3. SQLAlchemy 风格 URL（postgresql+asyncpg://）要改成 psycopg 认识的
   postgresql:// —— 本应用同步用 psycopg3，不用 asyncpg。
4. `row_factory=dict_row`：查出来是 dict，用 row["id"] 而不是 row[0]。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import psycopg
from psycopg.rows import dict_row

from config import settings


def build_dsn() -> str:
    """
    生成 psycopg 连接串（DSN）。

    返回示例：
      postgresql://user:pass@localhost:5432/aibase?connect_timeout=10
    """
    if settings.database_url:
        url = settings.database_url.strip()
        # 中台可能用 async 驱动前缀，这里统一剥掉
        url = url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgresql+psycopg://", "postgresql://"
        )
        return _ensure_query_params(
            url,
            sslmode=settings.db_sslmode or None,
            connect_timeout=settings.db_connect_timeout,
        )

    # 用户名密码可能含特殊字符，必须 quote_plus
    user = quote_plus(settings.db_user)
    password = quote_plus(settings.db_password)
    base = (
        f"postgresql://{user}:{password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )
    return _ensure_query_params(
        base,
        sslmode=settings.db_sslmode or None,
        connect_timeout=settings.db_connect_timeout,
    )


def _ensure_query_params(url: str, **params: object) -> str:
    """
    合并 URL query 参数；已有的键不覆盖（setdefault）。

    例如原 URL 已有 sslmode=require，则不会被空的 DB_SSLMODE 冲掉。
    """
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None or value == "":
            continue
        q.setdefault(key, str(value))
    return urlunparse(parsed._replace(query=urlencode(q)))


def connect():
    """
    打开一条同步连接。

    用法：
        with connect() as conn:
            conn.execute(...)
            conn.commit()
    """
    return psycopg.connect(
        build_dsn(),
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout,
    )
