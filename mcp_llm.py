"""
经中台 MCP `chat_completion` 调用大模型（询报价 / 共用）
======================================================

上层应用不再本地持有 DASHSCOPE_API_KEY；密钥与默认型号在中台配置。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class McpLlmError(RuntimeError):
    pass


def mcp_chat_available(*, project_id: str, mcp_token: str = "") -> bool:
    """有 PROJECT_ID 即可尝试；token 缺失时由 MCP 客户端报错。"""
    return bool((project_id or "").strip())


def chat_via_mcp(
    *,
    project_id: str,
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    client: Any = None,
) -> str:
    """
    调用中台 chat_completion，返回助手文本 content。
    """
    pid = (project_id or "").strip()
    if not pid:
        raise McpLlmError("未配置 PROJECT_ID")

    params: dict[str, Any] = {"project_id": pid}
    if messages is not None:
        params["messages"] = json.dumps(messages, ensure_ascii=False)
    if prompt:
        params["prompt"] = prompt
    if system_prompt:
        params["system_prompt"] = system_prompt
    if model:
        params["model"] = model
    if temperature is not None:
        params["temperature"] = float(temperature)
    if max_tokens is not None:
        params["max_tokens"] = int(max_tokens)

    if not params.get("messages") and not params.get("prompt"):
        raise McpLlmError("须提供 prompt 或 messages")

    try:
        if client is None:
            from mcp_client import build_mcp_client

            client = build_mcp_client()
        raw = client.call_tool("chat_completion", params)
    except Exception as exc:
        raise McpLlmError(f"MCP chat_completion 失败: {exc}") from exc

    text = (raw or "").strip()
    if not text:
        raise McpLlmError("大模型返回为空")
    if text.startswith("大模型调用失败"):
        raise McpLlmError(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        content = data.get("content")
        if content:
            return str(content).strip()
        err = data.get("error") or data.get("message")
        if err:
            raise McpLlmError(str(err))
    raise McpLlmError(f"无法解析大模型返回: {text[:200]}")
