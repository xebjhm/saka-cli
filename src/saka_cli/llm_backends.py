"""Model-agnostic OpenAI-compatible `LLMClient` for `saka_cli.kb`'s knowledge-base agent.

`OpenAICompatLLMClient` implements `pysaka.knowledge.llm.LLMClient` against any server
speaking the OpenAI `/v1/chat/completions` wire format -- cloud (Gemini's openai-compat
endpoint, OpenAI itself) and local (Ollama, llama.cpp) alike. It is the single adapter
`saka_cli.kb.KnowledgeBase` drives regardless of which backend a user selects; only
`base_url` / `model` / `api_key` differ between them.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

import aiohttp
import structlog

from pysaka.knowledge.llm import LLMResponse, ToolCall

logger = structlog.get_logger()

_DEFAULT_TIMEOUT = 120.0
_ERROR_BODY_SNIPPET_LEN = 500


class LLMBackendError(RuntimeError):
    """Raised when an OpenAI-compatible LLM backend returns a non-2xx HTTP response."""


class OpenAICompatLLMClient:
    """`LLMClient` adapter over any OpenAI-compatible `/chat/completions` endpoint.

    Works unmodified against cloud backends (Gemini's `v1beta/openai` compat endpoint,
    OpenAI itself) and local backends (Ollama, llama.cpp) -- only `base_url`/`model`/
    `api_key` change between them. `api_key` is optional since most local servers don't
    require one; when set, it's sent as `Authorization: Bearer <api_key>`.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse:
        openai_messages = _to_openai_messages(messages)
        openai_tools = _to_openai_tools(tools)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "temperature": 0.2,
        }
        if openai_tools:
            payload["tools"] = openai_tools

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body_snippet = (await resp.text())[:_ERROR_BODY_SNIPPET_LEN]
                    logger.warning(
                        "llm_backend.http_error",
                        url=url,
                        status=resp.status,
                        body=body_snippet,
                    )
                    raise LLMBackendError(
                        f"OpenAI-compatible LLM backend at {url} returned "
                        f"HTTP {resp.status}: {body_snippet}"
                    )
                data = await resp.json()

        return _parse_openai_response(data)


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translate the agent's generic message list into OpenAI `/chat/completions` messages.

    `tool_call_id` correlation: OpenAI ties an `assistant` message's `tool_calls[].id` to
    the following `tool` message(s)' `tool_call_id` by exact string match. The agent
    (`pysaka.knowledge.agent.KnowledgeAgent`) already threads a `ToolCall.id` through both
    the assistant turn and its matching tool result -- but that id can be empty (e.g. a
    `FakeLLMClient` script, or a real model that never sets one). When it's empty, we
    synthesize a stable per-call id (`call_<index-in-this-assistant-turn>`) and push it
    onto `pending_tool_call_ids`; the `tool` messages that immediately follow (one per
    call, same order -- see `KnowledgeAgent.ask`) pop from that queue so the assistant
    tool_call and its tool result always end up sharing the same id, synthesized or not.
    """
    openai_messages: list[dict] = []
    pending_tool_call_ids: deque[str] = deque()

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            openai_messages.append({"role": "system", "content": msg.get("content")})
        elif role == "user":
            openai_messages.append({"role": "user", "content": msg.get("content", "")})
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            if calls:
                openai_calls = []
                for i, call in enumerate(calls):
                    call_id = call.get("id") or f"call_{i}"
                    pending_tool_call_ids.append(call_id)
                    openai_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(
                                    call["arguments"], ensure_ascii=False
                                ),
                            },
                        }
                    )
                openai_messages.append(
                    {"role": "assistant", "content": None, "tool_calls": openai_calls}
                )
            else:
                openai_messages.append(
                    {"role": "assistant", "content": msg.get("content")}
                )
        elif role == "tool":
            original_id = msg.get("id") or ""
            synthesized_id = (
                pending_tool_call_ids.popleft() if pending_tool_call_ids else ""
            )
            tool_call_id = original_id or synthesized_id
            openai_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": msg.get("content", ""),
                }
            )

    return openai_messages


def _to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """Translate pysaka `TOOL_SCHEMAS`-shaped tools (`{name, description, parameters}`) to OpenAI's."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]


def _parse_openai_response(data: dict) -> LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        return LLMResponse(text=None, tool_calls=[])

    message = choices[0].get("message") or {}
    raw_tool_calls = message.get("tool_calls")
    if raw_tool_calls:
        tool_calls = [
            ToolCall(
                name=tc["function"]["name"],
                arguments=_parse_tool_arguments(tc["function"].get("arguments")),
                id=tc.get("id", ""),
            )
            for tc in raw_tool_calls
        ]
        return LLMResponse(text=None, tool_calls=tool_calls)

    return LLMResponse(text=message.get("content") or "", tool_calls=[])


def _parse_tool_arguments(arguments: Any) -> dict:
    """Some servers (llama.cpp) return `arguments` already parsed as an object, not a JSON string."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        return json.loads(arguments) if arguments else {}
    return {}
