"""Tests for `saka_cli.llm_backends.OpenAICompatLLMClient`.

The HTTP layer (`aiohttp.ClientSession`) is fully mocked with pure-Python fakes below --
no real network call is ever made. `_FakeSession`/`_FakeResponse` reproduce just enough of
aiohttp's async-context-manager surface (`async with session.post(...) as resp`) to drive
`OpenAICompatLLMClient.chat()` end to end and inspect exactly what it POSTed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pysaka.knowledge.llm import LLMClient
from pysaka.knowledge.tools import TOOL_SCHEMAS
from saka_cli.llm_backends import (
    LLMBackendError,
    OpenAICompatLLMClient,
    _parse_openai_response,
    _parse_tool_arguments,
    _to_openai_messages,
)


class _FakeResponse:
    """Stands in for `aiohttp.ClientResponse` as an async context manager."""

    def __init__(
        self, status: int, json_data: dict | None = None, text_data: str = ""
    ) -> None:
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stands in for `aiohttp.ClientSession` as an async context manager; records `.post()` calls."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.post_calls: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session(fake_session: _FakeSession):
    return patch(
        "saka_cli.llm_backends.aiohttp.ClientSession", return_value=fake_session
    )


# --- protocol conformance ----------------------------------------------------------------


def test_openai_compat_client_is_llm_client_protocol():
    client = OpenAICompatLLMClient(
        base_url="http://localhost:11434/v1", model="qwen2.5:14b"
    )
    assert isinstance(client, LLMClient)


# --- request building ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_with_tools_builds_openai_request_and_parses_tool_calls_string_args():
    response_payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"query": "test"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    fake_session = _FakeSession(_FakeResponse(200, json_data=response_payload))

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(
            base_url="http://localhost:11434/v1", model="qwen2.5:14b"
        )
        resp = await client.chat(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
            ],
            tools=TOOL_SCHEMAS,
        )

    assert resp.text is None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "search"
    assert resp.tool_calls[0].arguments == {"query": "test"}
    assert resp.tool_calls[0].id == "call_abc"

    sent = fake_session.post_calls[0]
    assert sent["url"] == "http://localhost:11434/v1/chat/completions"
    body = sent["json"]
    assert body["model"] == "qwen2.5:14b"
    assert body["temperature"] == 0.2
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == TOOL_SCHEMAS[0]["name"]
    assert body["tools"][0]["function"]["parameters"] == TOOL_SCHEMAS[0]["parameters"]


@pytest.mark.asyncio
async def test_chat_parses_tool_call_arguments_as_object():
    """Some servers (llama.cpp) return `arguments` as an already-parsed object, not a string."""
    response_payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": {"query": "obj"},
                            },
                        }
                    ],
                }
            }
        ]
    }
    fake_session = _FakeSession(_FakeResponse(200, json_data=response_payload))

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(
            base_url="http://localhost:8080/v1", model="local-model"
        )
        resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.tool_calls[0].arguments == {"query": "obj"}
    assert resp.tool_calls[0].name == "search"


@pytest.mark.asyncio
async def test_chat_plain_text_response():
    response_payload = {
        "choices": [{"message": {"role": "assistant", "content": "hello there"}}]
    }
    fake_session = _FakeSession(_FakeResponse(200, json_data=response_payload))

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-2.5-flash",
            api_key="secret",
        )
        resp = await client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "hello there"
    assert resp.tool_calls == []

    sent = fake_session.post_calls[0]
    assert sent["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_chat_omits_authorization_header_when_no_api_key():
    fake_session = _FakeSession(
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "ok"}}]})
    )

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(base_url="http://localhost:11434/v1", model="m")
        await client.chat([{"role": "user", "content": "hi"}])

    headers = fake_session.post_calls[0]["headers"]
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_chat_without_tools_omits_tools_key():
    fake_session = _FakeSession(
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "ok"}}]})
    )

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(base_url="http://localhost:11434/v1", model="m")
        await client.chat([{"role": "user", "content": "hi"}])

    assert "tools" not in fake_session.post_calls[0]["json"]


# --- multi-turn tool_call_id correlation ----------------------------------------------------


@pytest.mark.asyncio
async def test_chat_multi_turn_synthesizes_and_correlates_tool_call_ids():
    fake_session = _FakeSession(
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "final"}}]})
    )

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(
            base_url="http://localhost:11434/v1", model="qwen2.5:14b"
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "search", "arguments": {"query": "x"}, "id": ""},
                    {"name": "get_document", "arguments": {"doc_id": "d1"}, "id": ""},
                ],
            },
            {"role": "tool", "name": "search", "id": "", "content": '{"hits": []}'},
            {
                "role": "tool",
                "name": "get_document",
                "id": "",
                "content": '{"doc_id": "d1"}',
            },
        ]
        await client.chat(messages)

    body = fake_session.post_calls[0]["json"]
    assistant_msg = body["messages"][2]
    tool_msg_1 = body["messages"][3]
    tool_msg_2 = body["messages"][4]

    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] is None
    ids = [tc["id"] for tc in assistant_msg["tool_calls"]]
    assert ids[0] and ids[1] and ids[0] != ids[1]
    assert tool_msg_1["role"] == "tool"
    assert tool_msg_1["tool_call_id"] == ids[0]
    assert tool_msg_2["tool_call_id"] == ids[1]
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"]) == {
        "query": "x"
    }
    assert assistant_msg["tool_calls"][1]["function"]["name"] == "get_document"


@pytest.mark.asyncio
async def test_chat_reuses_existing_tool_call_ids_when_present():
    fake_session = _FakeSession(
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "final"}}]})
    )

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(base_url="http://localhost:11434/v1", model="m")
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"name": "search", "arguments": {}, "id": "call-1"}],
            },
            {"role": "tool", "name": "search", "id": "call-1", "content": "{}"},
        ]
        await client.chat(messages)

    body = fake_session.post_calls[0]["json"]
    assert body["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert body["messages"][1]["tool_call_id"] == "call-1"


# --- error handling -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_raises_clear_error_on_http_failure():
    fake_session = _FakeSession(_FakeResponse(500, text_data="internal error"))

    with _patch_session(fake_session):
        client = OpenAICompatLLMClient(base_url="http://localhost:11434/v1", model="m")
        with pytest.raises(LLMBackendError) as exc_info:
            await client.chat([{"role": "user", "content": "hi"}])

    assert "500" in str(exc_info.value)
    assert "internal error" in str(exc_info.value)


# --- small pure-function edge cases (internal helpers) ---------------------------------------


def test_to_openai_messages_passes_through_plain_assistant_content():
    """An assistant message with no `tool_calls` (a prior final-answer turn in `history`)
    is passed through as a normal `{role, content}` message."""
    result = _to_openai_messages([{"role": "assistant", "content": "previous answer"}])
    assert result == [{"role": "assistant", "content": "previous answer"}]


def test_parse_openai_response_with_no_choices_returns_empty_response():
    resp = _parse_openai_response({"choices": []})
    assert resp.text is None
    assert resp.tool_calls == []


def test_parse_tool_arguments_returns_empty_dict_for_unexpected_type():
    assert _parse_tool_arguments(None) == {}
    assert _parse_tool_arguments(123) == {}
