"""Tests for `saka_cli.kb` -- the local knowledge-base harness over synced data.

Uses pure-Python fakes (`FakeEmbedder`, `FakeVectorStore`, `pysaka.knowledge.llm.FakeLLMClient`)
so these tests never touch a real ONNX model, numpy vector math, or the network. The
real `OnnxEmbedder` / `NumpyVectorStore` / `OpenAICompatLLMClient` backends are exercised
manually only (see task-17-report.md / the LLM backend report).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pysaka.knowledge.llm import FakeLLMClient, LLMResponse, ToolCall
from saka_cli.kb import (
    KnowledgeBase,
    default_cloud_llm,
    default_local_llm,
    run_kb_command,
)
from saka_cli.llm_backends import OpenAICompatLLMClient

_SERVICE = "hinatazaka46"
_DISPLAY = "日向坂46"

_MEMBERS = {
    "meta": {"group": _SERVICE},
    "members": [
        {
            "blogId": "12",
            "nameKanji": "金村 美玖",
            "nameHiragana": "かねむら みく",
            "nameRomaji": "Kanemura Miku",
            "generation": 2,
            "status": "active",
        }
    ],
}

_EXPECTED_MSG_DOC_ID = "msg:hinatazaka46:hinatazaka46:12:1"

# --- call-names fixtures: given-name collision mirroring the real 蔵盛/上村 -> ひなの case --
# 蔵盛妃那乃 and 上村ひなの share the hiragana given name "ひなの", so the mechanically-seeded
# AliasTable maps "ひなの" to *both* of them -- ambiguous by design. A curated call_names.json
# edge records that 金村美玖 (the caller) specifically addresses 蔵盛妃那乃 (the callee) by that
# name, which is what should resolve the ambiguity once threaded into `MentionDetector`.
_COLLISION_CALLER_ID = f"{_SERVICE}:12"  # 金村 美玖
_COLLISION_CALLEE_ID = (
    f"{_SERVICE}:1"  # 蔵盛 妃那乃 -- named by the curated call-name edge
)
_COLLISION_NAMESAKE_ID = f"{_SERVICE}:2"  # 上村 ひなの -- shares "ひなの", not curated

_COLLISION_MEMBERS = {
    "meta": {"group": _SERVICE},
    "members": [
        {
            "blogId": "12",
            "nameKanji": "金村 美玖",
            "nameHiragana": "かねむら みく",
            "nameRomaji": "Kanemura Miku",
            "generation": 2,
            "status": "active",
        },
        {
            "blogId": "1",
            "nameKanji": "蔵盛 妃那乃",
            "nameHiragana": "くらもり ひなの",
            "nameRomaji": "Kuramochi Hinano",
            "generation": 4,
            "status": "active",
        },
        {
            "blogId": "2",
            "nameKanji": "上村 ひなの",
            "nameHiragana": "かみむら ひなの",
            "nameRomaji": "Kamimura Hinano",
            "generation": 4,
            "status": "active",
        },
    ],
}

_CALL_NAMES_DATA = {
    "edges": [
        {
            "caller_id": _COLLISION_CALLER_ID,
            "caller_name": "金村 美玖",
            "callee_id": _COLLISION_CALLEE_ID,
            "callee_name": "蔵盛 妃那乃",
            "names": ["ひなの"],
        }
    ]
}


class FakeEmbedder:
    """Deterministic fixed-dim embedder: hashes any text into a vector.

    No model/network -- good enough for indexing/retrieval structure tests where the
    real BM25 lexical index (not embedding quality) is what surfaces the match.
    """

    dim = 4

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        return [_vector(text) for text in texts]


def _vector(text: str) -> list[float]:
    seed = sum(ord(c) for c in text) or 1
    return [float((seed * (i + 1)) % 97) for i in range(FakeEmbedder.dim)]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """Pure-Python cosine-similarity vector store (no numpy), copied in spirit from
    pysaka's tests/knowledge/test_retrieve.py."""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}

    def add(self, ids: list[str], vectors: list[list[float]]) -> None:
        for chunk_id, vector in zip(ids, vectors):
            self._vectors[chunk_id] = vector

    def remove(self, ids: list[str]) -> None:
        for chunk_id in ids:
            self._vectors.pop(chunk_id, None)

    def search(
        self, vector: list[float], k: int, allowed_ids: set[str] | None = None
    ) -> list[tuple[str, float]]:
        candidate_ids = (
            self._vectors.keys()
            if allowed_ids is None
            else self._vectors.keys() & allowed_ids
        )
        scored = [(cid, _cosine(vector, self._vectors[cid])) for cid in candidate_ids]
        scored = [(cid, score) for cid, score in scored if score > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


@pytest.fixture
def members_dir(tmp_path: Path) -> Path:
    d = tmp_path / "members"
    d.mkdir()
    (d / f"{_SERVICE}.json").write_text(
        json.dumps(_MEMBERS, ensure_ascii=False), encoding="utf-8"
    )
    return d


@pytest.fixture
def collision_members_dir(tmp_path: Path) -> Path:
    d = tmp_path / "collision_members"
    d.mkdir()
    (d / f"{_SERVICE}.json").write_text(
        json.dumps(_COLLISION_MEMBERS, ensure_ascii=False), encoding="utf-8"
    )
    return d


def _write_messages_fixture(tmp_path: Path) -> None:
    d = tmp_path / _DISPLAY / "messages" / "34 金村 美玖" / "58 金村 美玖"
    d.mkdir(parents=True)
    (d / "messages.json").write_text(
        json.dumps(
            {
                "member": {"id": 58, "name": "金村 美玖", "group_id": 34},
                "messages": [
                    {
                        "id": 1,
                        "timestamp": "2025-08-11T17:00:33Z",
                        "type": "text",
                        "is_favorite": False,
                        "content": "焼肉たべた",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_collision_message_fixture(tmp_path: Path) -> None:
    """A message from 金村美玖 (the caller) mentioning "ひなの" -- ambiguous between
    蔵盛妃那乃 and 上村ひなの unless a `CallNameTable` resolves it directionally."""
    d = tmp_path / _DISPLAY / "messages" / "12 金村 美玖" / "12 金村 美玖"
    d.mkdir(parents=True)
    (d / "messages.json").write_text(
        json.dumps(
            {
                "member": {"id": 12, "name": "金村 美玖", "group_id": 12},
                "messages": [
                    {
                        "id": 501,
                        "timestamp": "2025-09-01T10:00:00Z",
                        "type": "text",
                        "is_favorite": False,
                        "content": "ひなのと話した",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _build_collision_kb(
    tmp_path: Path, collision_members_dir: Path, call_names_file: Path | None = None
) -> KnowledgeBase:
    return KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=collision_members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        call_names_file=call_names_file,
    )


def _mentions_from_caller_doc(kb: KnowledgeBase) -> list[str]:
    doc = next(d for d in kb.store.all() if d.author_id == _COLLISION_CALLER_ID)
    return doc.mentions


def _write_blog_fixture(tmp_path: Path) -> None:
    d = tmp_path / _DISPLAY / "blogs" / "金村 美玖" / "20250811_99"
    d.mkdir(parents=True)
    (d / "blog.json").write_text(
        json.dumps(
            {
                "meta": {
                    "id": 99,
                    "member_name": "金村 美玖",
                    "published_at": "2025-08-11T09:00:00Z",
                },
                "content": {"html": "<p>今日はブログを書きました。</p>"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# --- build_index ----------------------------------------------------------------


def test_build_index_discovers_messages(tmp_path: Path, members_dir: Path):
    _write_messages_fixture(tmp_path)

    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    count = kb.build_index()

    assert count == 1
    assert (
        kb.store.get("msg:hinatazaka46:hinatazaka46:auto") is None
    )  # sanity: no such doc
    assert any(doc.text == "焼肉たべた" for doc in kb.store.all())

    doc = kb.store.get(_EXPECTED_MSG_DOC_ID)
    assert doc is not None
    assert doc.text == "焼肉たべた"
    assert doc.author_id == "hinatazaka46:12"  # resolved via members.json roster
    assert doc.type == "text_msg"


def test_build_index_discovers_blogs(tmp_path: Path, members_dir: Path):
    _write_blog_fixture(tmp_path)

    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    count = kb.build_index()

    assert count == 1
    docs = kb.store.all()
    assert len(docs) == 1
    assert docs[0].type == "blog"
    assert "ブログを書きました" in docs[0].text
    assert docs[0].author_id == "hinatazaka46:12"


def test_build_index_persists_document_store(tmp_path: Path, members_dir: Path):
    _write_messages_fixture(tmp_path)

    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    kb.build_index()

    persisted = tmp_path / ".kb" / _SERVICE / "documents.json"
    assert persisted.exists()
    data = json.loads(persisted.read_text(encoding="utf-8"))
    assert len(data["documents"]) == 1
    assert data["documents"][0]["text"] == "焼肉たべた"


def test_build_index_with_no_data_returns_zero(tmp_path: Path, members_dir: Path):
    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    assert kb.build_index() == 0
    assert kb.store.all() == []


# --- call_names_file wiring (directional CallNameTable -> MentionDetector) --------


def test_call_names_file_resolves_ambiguous_alias_to_specific_callee(
    tmp_path: Path, collision_members_dir: Path
):
    """WITH a call_names.json: `KnowledgeBase` builds a `CallNameTable` and passes it into
    `MentionDetector`, so 蔵盛妃那乃/上村ひなの's shared "ひなの" reading resolves
    deterministically to the callee the curated edge names, not both namesakes."""
    _write_collision_message_fixture(tmp_path)
    call_names_path = tmp_path / "call_names.json"
    call_names_path.write_text(
        json.dumps(_CALL_NAMES_DATA, ensure_ascii=False), encoding="utf-8"
    )

    kb = _build_collision_kb(
        tmp_path, collision_members_dir, call_names_file=call_names_path
    )
    kb.build_index()

    assert _mentions_from_caller_doc(kb) == [_COLLISION_CALLEE_ID]


def test_without_call_names_file_ambiguous_alias_stays_ambiguous(
    tmp_path: Path, collision_members_dir: Path
):
    """Control: the SAME setup but no `call_names_file` -- the given-name collision between
    蔵盛妃那乃 and 上村ひなの is left ambiguous (both candidates kept), proving the wiring
    exercised above is what actually changes detection behavior."""
    _write_collision_message_fixture(tmp_path)

    kb = _build_collision_kb(tmp_path, collision_members_dir)
    kb.build_index()

    assert _mentions_from_caller_doc(kb) == sorted(
        [_COLLISION_CALLEE_ID, _COLLISION_NAMESAKE_ID]
    )


def test_run_kb_command_passes_call_names_file_to_knowledge_base(
    tmp_path: Path, members_dir: Path
):
    call_names_path = tmp_path / "call_names.json"
    with patch("saka_cli.kb.KnowledgeBase") as MockKB:
        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask=None,
            kb_index=True,
            kb_format="text",
            call_names_file=call_names_path,
        )

    assert MockKB.call_args.kwargs["call_names_file"] == call_names_path


def test_run_kb_command_leaves_call_names_file_none_by_default(
    tmp_path: Path, members_dir: Path
):
    """No `call_names_file` passed: `KnowledgeBase` gets `call_names_file=None` -- unchanged
    default behavior for callers that never touch this flag."""
    with patch("saka_cli.kb.KnowledgeBase") as MockKB:
        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask=None,
            kb_index=True,
            kb_format="text",
        )

    assert MockKB.call_args.kwargs["call_names_file"] is None


# --- ask --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_returns_validated_answer_with_citation(
    tmp_path: Path, members_dir: Path
):
    _write_messages_fixture(tmp_path)

    llm = FakeLLMClient(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        name="search", arguments={"query": "焼肉たべた"}, id="call-1"
                    )
                ],
            ),
            LLMResponse(
                text=json.dumps(
                    {
                        "sentences": [
                            {
                                "text": "焼肉たべた",
                                "citation_ids": [_EXPECTED_MSG_DOC_ID],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )

    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        llm=llm,
    )
    kb.build_index()

    answer = await kb.ask("金村美玖は何を食べましたか？")

    assert answer.no_evidence is False
    assert len(answer.citations) == 1
    assert answer.citations[0].doc_id == _EXPECTED_MSG_DOC_ID
    assert answer.citations[0].quoted_snippet.startswith("焼肉たべた")


@pytest.mark.asyncio
async def test_ask_with_no_evidence_script_returns_no_evidence(
    tmp_path: Path, members_dir: Path
):
    _write_messages_fixture(tmp_path)

    llm = FakeLLMClient([LLMResponse(text=json.dumps({"no_evidence": True}))])

    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        llm=llm,
    )
    kb.build_index()

    answer = await kb.ask("誰も知らないことは何ですか？")

    assert answer.no_evidence is True
    assert answer.citations == []


# --- run_kb_command (mocked KnowledgeBase -- real backends are manual-only) --------


def test_run_kb_command_builds_index_and_does_not_ask(
    tmp_path: Path, members_dir: Path
):
    with patch("saka_cli.kb.KnowledgeBase") as MockKB:
        instance = MockKB.return_value
        instance.build_index.return_value = 3

        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask=None,
            kb_index=True,
            kb_format="text",
        )

        instance.build_index.assert_called_once()
        instance.ask.assert_not_called()


def test_run_kb_command_ask_json_format_prints_payload(
    tmp_path: Path, members_dir: Path, capsys
):
    from datetime import datetime, timezone

    from pysaka.knowledge.models import Answer, AnswerSentence, Citation, SourceRef

    citation = Citation(
        doc_id=_EXPECTED_MSG_DOC_ID,
        source_ref=SourceRef(
            service=_SERVICE, kind="message", group_id=34, member_id=58, message_id=1
        ),
        quoted_snippet="焼肉たべた",
        member="hinatazaka46:12",
        timestamp=datetime(2025, 8, 11, 17, 0, 33, tzinfo=timezone.utc),
    )
    answer = Answer(
        sentences=[
            AnswerSentence(text="焼肉たべた", citation_ids=[_EXPECTED_MSG_DOC_ID])
        ],
        citations=[citation],
        no_evidence=False,
    )

    with (
        patch("saka_cli.kb.KnowledgeBase") as MockKB,
        patch("saka_cli.kb.logger"),
    ):
        instance = MockKB.return_value
        instance.ask = AsyncMock(return_value=answer)
        instance.registry.get.return_value = None

        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask="金村美玖は何を食べましたか？",
            kb_index=False,
            kb_format="json",
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["no_evidence"] is False
    assert payload["citations"][0]["doc_id"] == _EXPECTED_MSG_DOC_ID
    assert payload["citations"][0]["snippet"] == "焼肉たべた"


def test_run_kb_command_ask_no_evidence_logs_message(
    tmp_path: Path, members_dir: Path, capsys
):
    from pysaka.knowledge.models import Answer

    answer = Answer(sentences=[], citations=[], no_evidence=True)

    with (
        patch("saka_cli.kb.KnowledgeBase") as MockKB,
        patch("saka_cli.kb.logger") as mock_logger,
    ):
        instance = MockKB.return_value
        instance.ask = AsyncMock(return_value=answer)

        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask="誰も知らないことは何ですか？",
            kb_index=False,
            kb_format="text",
        )

    # no_evidence goes through logger.info (i18n string), never print() for text format
    mock_logger.info.assert_any_call("No evidence found for this question.")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_run_kb_command_ask_without_index_warns(tmp_path: Path, members_dir: Path):
    from pysaka.knowledge.models import Answer

    answer = Answer(sentences=[], citations=[], no_evidence=True)

    with (
        patch("saka_cli.kb.KnowledgeBase") as MockKB,
        patch("saka_cli.kb.logger") as mock_logger,
    ):
        instance = MockKB.return_value
        instance.ask = AsyncMock(return_value=answer)

        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask="テスト",
            kb_index=False,
            kb_format="text",
        )

    # Verify warning is logged when ask is set but kb_index is not
    mock_logger.warning.assert_called_once()
    warning_call = mock_logger.warning.call_args[0][0]
    assert (
        "No index was built this run" in warning_call
        or "インデックスが構築されていません" in warning_call
        or "未構建索引" in warning_call
        or "冇構建索引" in warning_call
    )


# --- default_cloud_llm / default_local_llm / _resolve_llm wiring ------------------


def test_default_cloud_llm_uses_gemini_openai_compat_endpoint(monkeypatch):
    monkeypatch.delenv("SAKA_KB_LLM_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")

    client = default_cloud_llm()

    assert isinstance(client, OpenAICompatLLMClient)
    assert client._base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert client._model == "gemini-2.5-flash"
    assert client._api_key == "env-key"


def test_default_cloud_llm_honors_explicit_model_and_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    client = default_cloud_llm(model="gemini-1.5-pro", api_key="explicit-key")

    assert client._model == "gemini-1.5-pro"
    assert client._api_key == "explicit-key"


def test_default_local_llm_defaults_to_ollama():
    client = default_local_llm()

    assert isinstance(client, OpenAICompatLLMClient)
    assert client._base_url == "http://localhost:11434/v1"
    assert client._model == "qwen2.5:14b"
    assert client._api_key is None


def test_default_local_llm_honors_overrides():
    client = default_local_llm(base_url="http://localhost:8080/v1", model="llama3")

    assert client._base_url == "http://localhost:8080/v1"
    assert client._model == "llama3"


def test_ensure_llm_defaults_to_cloud_when_none_injected(
    tmp_path: Path, members_dir: Path
):
    kb = KnowledgeBase(
        tmp_path,
        _SERVICE,
        members_dir=members_dir,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )

    llm = kb._ensure_llm()

    assert isinstance(llm, OpenAICompatLLMClient)
    assert llm._base_url == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_run_kb_command_resolves_explicit_local_backend(
    tmp_path: Path, members_dir: Path
):
    with patch("saka_cli.kb.KnowledgeBase") as MockKB:
        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask=None,
            kb_index=True,
            kb_format="text",
            llm_backend="local",
            llm_base_url="http://localhost:8080/v1",
            llm_model="llama3",
        )

    llm = MockKB.call_args.kwargs["llm"]
    assert isinstance(llm, OpenAICompatLLMClient)
    assert llm._base_url == "http://localhost:8080/v1"
    assert llm._model == "llama3"


def test_run_kb_command_leaves_llm_none_when_backend_unspecified(
    tmp_path: Path, members_dir: Path
):
    """No --llm-backend/--llm-base-url/--llm-model: KnowledgeBase gets `llm=None` and lazily
    builds its own cloud default on first actual use -- unchanged default behavior."""
    with patch("saka_cli.kb.KnowledgeBase") as MockKB:
        run_kb_command(
            output_dir=tmp_path,
            service=_SERVICE,
            members_dir=members_dir,
            ask=None,
            kb_index=True,
            kb_format="text",
        )

    assert MockKB.call_args.kwargs["llm"] is None
