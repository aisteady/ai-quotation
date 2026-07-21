"""
启动入口（start.py）
====================

学习要点
--------
1. 不直接 `streamlit run`，而是用本脚本读 Settings 再拼命令，
   保证 PORT / ADDRESS 与 `.env` 一致。
2. `subprocess.call` 会阻塞直到 Streamlit 退出；退出码原样返回。
3. 用法：在 `models/ai_quotation` 下执行 `python start.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from config import settings, validate_settings


def main() -> int:
    # 先做配置自检（开发模式多为 warning）
    validate_settings(strict=False)
    env = os.environ.copy()
    # 同步给 Streamlit 内置环境变量（部分插件会读它们）
    env.setdefault("STREAMLIT_SERVER_PORT", str(settings.port))
    env.setdefault("STREAMLIT_SERVER_ADDRESS", settings.server_address)
    cmd = [
        sys.executable,  # 当前 venv 的 python，避免跑到系统解释器
        "-m",
        "streamlit",
        "run",
        str(_APP_DIR / "app.py"),
        "--server.port",
        str(settings.port),
        "--server.address",
        settings.server_address,
    ]
    return subprocess.call(cmd, cwd=str(_APP_DIR), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
