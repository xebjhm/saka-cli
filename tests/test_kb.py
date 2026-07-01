"""Tests for `saka_cli.kb` -- the local knowledge-base harness over synced data.

Uses pure-Python fakes (`FakeEmbedder`, `FakeVectorStore`, `pysaka.knowledge.llm.FakeLLMClient`)
so these tests never touch a real ONNX model, numpy vector math, or the network. The
real `OnnxEmbedder` / `NumpyVectorStore` / `GeminiLLMClient` backends are exercised
manually only (see task-17-report.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pysaka.knowledge.llm import FakeLLMClient, LLMResponse, ToolCall
from saka_cli.kb import KnowledgeBase, run_kb_command

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

    with patch("saka_cli.kb.KnowledgeBase") as MockKB:
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
