"""Local knowledge-base harness over synced `pysaka` output.

Wraps `pysaka.knowledge` (registry, aliases, ingest, chunking, store, retrieve,
tools, agent, validator) into a single `KnowledgeBase` object that `saka-cli`'s
`--kb-index` / `--ask` flags drive: scan a group's synced `output/<display>/` tree,
build a searchable index, and answer natural-language questions with cited evidence.

The default backends (`OnnxEmbedder`, `NumpyVectorStore`, `GeminiLLMClient`) are
lazily constructed on first use so importing this module -- or building a
`KnowledgeBase` with fakes injected for tests -- never touches a model file or the
network. `pysaka.knowledge.backends` requires the `pysaka[embeddings]` extra.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp
import structlog

from pysaka import Group
from pysaka.client import GROUP_CONFIG
from pysaka.knowledge.agent import KnowledgeAgent
from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.chunking import chunk_documents
from pysaka.knowledge.ingest import ingest_blog, ingest_messages
from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.llm import LLMResponse, ToolCall
from pysaka.knowledge.mentions import MentionDetector
from pysaka.knowledge.models import Answer, Citation, Document, Scope, SourceRef
from pysaka.knowledge.registry import MemberRegistry
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.tools import ToolRunner
from pysaka.knowledge.validator import validate

from saka_cli.strings import get_string

logger = structlog.get_logger()

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
_SNIPPET_LEN = 160


class GeminiLLMClient:
    """Minimal `LLMClient` adapter over Google's Gemini `generateContent` REST API.

    Deliberately small: just enough request/response mapping to drive
    `KnowledgeAgent`'s tool-calling loop against Gemini. Reads the API key from the
    `GEMINI_API_KEY` env var. NOT unit-tested here -- it needs a live network call and
    a real key; `tests/test_kb.py` exercises `KnowledgeBase.ask()` via the injected
    `pysaka.knowledge.llm.FakeLLMClient` instead.
    """

    def __init__(
        self, model: str = _DEFAULT_GEMINI_MODEL, api_key: str | None = None
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse:
        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY is not set; cannot call the Gemini API.")
        payload = _to_gemini_payload(messages, tools)
        url = _GEMINI_ENDPOINT.format(model=self._model)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, params={"key": self._api_key}, json=payload
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return _parse_gemini_response(data)


def _to_gemini_payload(messages: list[dict], tools: list[dict] | None) -> dict:
    """Map the agent's generic `messages`/`tools` shape to a Gemini `generateContent` body."""
    system_text: str | None = None
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_text = msg.get("content")
        elif role == "user":
            contents.append(
                {"role": "user", "parts": [{"text": msg.get("content", "")}]}
            )
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            if calls:
                parts = [
                    {"functionCall": {"name": c["name"], "args": c["arguments"]}}
                    for c in calls
                ]
            else:
                parts = [{"text": msg.get("content", "")}]
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            contents.append(
                {
                    "role": "function",
                    "parts": [{"functionResponse": _tool_response(msg)}],
                }
            )
    payload: dict = {"contents": contents}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]
    return payload


def _tool_response(msg: dict) -> dict:
    content = msg.get("content", "")
    try:
        response = json.loads(content)
    except (TypeError, ValueError):
        response = {"content": content}
    return {"name": msg.get("name", ""), "response": response}


def _parse_gemini_response(data: dict) -> LLMResponse:
    candidates = data.get("candidates") or []
    if not candidates:
        return LLMResponse(text=None, tool_calls=[])
    parts = candidates[0].get("content", {}).get("parts", [])
    tool_calls: list[ToolCall] = []
    text_parts: list[str] = []
    for i, part in enumerate(parts):
        function_call = part.get("functionCall")
        if function_call:
            tool_calls.append(
                ToolCall(
                    name=function_call.get("name", ""),
                    arguments=function_call.get("args") or {},
                    id=f"call-{i}",
                )
            )
        elif part.get("text"):
            text_parts.append(part["text"])
    if tool_calls:
        return LLMResponse(text=None, tool_calls=tool_calls)
    return LLMResponse(text="".join(text_parts) or None, tool_calls=[])


class KnowledgeBase:
    """Ties `pysaka.knowledge` together for one group's synced `output_dir` tree.

    `embedder` / `vector_store` / `llm` are injectable (tests pass pure-Python fakes);
    left as `None` they are lazily constructed from the real backends
    (`OnnxEmbedder` reading `SAKA_KB_MODEL_DIR`, `NumpyVectorStore`, `GeminiLLMClient`)
    on first actual use, so building a `KnowledgeBase` never requires a model file or
    network access unless `build_index()`/`ask()` is actually called without fakes.
    """

    def __init__(
        self,
        output_dir: Path,
        service: str,
        *,
        members_dir: Path,
        embedder: Any = None,
        vector_store: Any = None,
        llm: Any = None,
        aliases_file: Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.service = service
        self.members_dir = Path(members_dir)

        self._embedder = embedder
        self._vector_store = vector_store
        self._llm = llm
        self._retriever: HybridRetriever | None = None

        self._display_name = GROUP_CONFIG[Group(service)]["display_name"]

        members_path = self.members_dir / f"{service}.json"
        with open(members_path, encoding="utf-8") as f:
            members_data = json.load(f)
        self.registry = MemberRegistry.from_members_json(members_data, service)

        self.aliases = AliasTable.seed_from_registry(self.registry)
        if aliases_file is not None:
            with open(Path(aliases_file), encoding="utf-8") as f:
                curated = json.load(f)
            self.aliases.load_curated(curated)

        self._mention_detector = MentionDetector(self.aliases.entries(service))
        self.store = DocumentStore()

    # --- lazy backend construction ------------------------------------------------

    def _ensure_embedder(self) -> Any:
        if self._embedder is None:
            from pysaka.knowledge.backends.onnx_embedder import OnnxEmbedder

            model_dir = os.environ.get("SAKA_KB_MODEL_DIR")
            if not model_dir:
                raise RuntimeError(
                    "SAKA_KB_MODEL_DIR is not set; cannot construct the default OnnxEmbedder "
                    "(set it to a directory containing model.onnx + tokenizer.json, or inject "
                    "`embedder=` for testing)."
                )
            self._embedder = OnnxEmbedder(Path(model_dir))
        return self._embedder

    def _ensure_vector_store(self) -> Any:
        if self._vector_store is None:
            from pysaka.knowledge.backends.numpy_store import NumpyVectorStore

            self._vector_store = NumpyVectorStore()
        return self._vector_store

    def _ensure_llm(self) -> Any:
        if self._llm is None:
            self._llm = GeminiLLMClient()
        return self._llm

    def _ensure_retriever(self) -> HybridRetriever:
        if self._retriever is None:
            self._retriever = HybridRetriever(
                self.store,
                PureLexicalIndex(),
                self._ensure_vector_store(),
                self._ensure_embedder(),
            )
        return self._retriever

    # --- indexing -------------------------------------------------------------

    def build_index(self) -> int:
        """Discover + ingest synced messages/blogs, index them, and persist the store.

        Returns the number of documents indexed.
        """
        docs: list[Document] = []

        messages_root = self.output_dir / self._display_name / "messages"
        for path in sorted(messages_root.rglob("messages.json")):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            docs.extend(
                ingest_messages(data, self.service, self.registry.resolve_author)
            )

        blogs_root = self.output_dir / self._display_name / "blogs"
        for path in sorted(blogs_root.rglob("blog.json")):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            docs.append(ingest_blog(data, self.service, self.registry.resolve_author))

        for doc in docs:
            doc.mentions = self._mention_detector.detect(doc.text, doc.author_id)

        self.store.upsert(docs)
        chunks = chunk_documents(docs)
        self._ensure_retriever().index(chunks)
        self._persist_store()

        logger.info(
            "kb.build_index.done",
            service=self.service,
            documents=len(docs),
            chunks=len(chunks),
        )
        return len(docs)

    def _persist_store(self) -> None:
        """Best-effort: write the document store to `output_dir/.kb/<service>/documents.json`."""
        kb_dir = self.output_dir / ".kb" / self.service
        try:
            kb_dir.mkdir(parents=True, exist_ok=True)
            self.store.save_json(kb_dir / "documents.json")
        except OSError as exc:
            logger.warning("kb.persist_store.failed", path=str(kb_dir), error=str(exc))

    # --- querying ---------------------------------------------------------------

    async def ask(self, question: str) -> Answer:
        """Answer `question` via the bounded tool-calling agent, then grounding-validate it."""
        retriever = self._ensure_retriever()
        tool_runner = ToolRunner(self.aliases, self.registry, retriever, self.store)
        agent = KnowledgeAgent(self._ensure_llm(), tool_runner)
        answer, surfaced = await agent.ask(question, Scope(service=self.service))
        return validate(answer, surfaced, self.store)


# --- rendering ------------------------------------------------------------------


def _member_name(kb: KnowledgeBase, canonical_id: str) -> str:
    member = kb.registry.get(canonical_id)
    return member.name if member is not None else canonical_id


def _source_ref_str(ref: SourceRef) -> str:
    if ref.kind == "blog":
        return f"blog:{ref.service}:{ref.blog_id}"
    return f"message:{ref.service}:{ref.group_id}:{ref.member_id}:{ref.message_id}"


def _citation_line(kb: KnowledgeBase, citation: Citation) -> str:
    date = citation.timestamp.date().isoformat()
    member = _member_name(kb, citation.member)
    snippet = citation.quoted_snippet[:_SNIPPET_LEN]
    return f"{member} · {date} · {snippet} · {_source_ref_str(citation.source_ref)}"


def _citation_dict(kb: KnowledgeBase, citation: Citation) -> dict:
    return {
        "doc_id": citation.doc_id,
        "member_id": citation.member,
        "member_name": _member_name(kb, citation.member),
        "date": citation.timestamp.date().isoformat(),
        "snippet": citation.quoted_snippet[:_SNIPPET_LEN],
        "source_ref": asdict(citation.source_ref),
    }


def _render_answer(kb: KnowledgeBase, answer: Answer, kb_format: str) -> None:
    if answer.no_evidence:
        logger.info(get_string("kb_no_evidence"))
        if kb_format == "json":
            print(
                json.dumps(
                    {"no_evidence": True, "sentences": [], "citations": []},
                    ensure_ascii=False,
                )
            )
        return

    if kb_format == "json":
        payload = {
            "no_evidence": False,
            "sentences": [
                {"text": s.text, "citation_ids": s.citation_ids}
                for s in answer.sentences
            ],
            "citations": [_citation_dict(kb, c) for c in answer.citations],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    if kb_format == "text":
        for sentence in answer.sentences:
            logger.info(sentence.text)
    for citation in answer.citations:
        logger.info(_citation_line(kb, citation))


def run_kb_command(
    output_dir: Path,
    service: str,
    members_dir: Path,
    ask: str | None,
    kb_index: bool,
    kb_format: str,
    aliases_file: Path | None = None,
) -> None:
    """Entry point wired from `saka_cli.cli.main()`: build the index and/or answer a question."""
    kb = KnowledgeBase(
        Path(output_dir),
        service,
        members_dir=Path(members_dir),
        aliases_file=aliases_file,
    )

    if kb_index:
        logger.info(get_string("kb_index_start").format(service))
        count = kb.build_index()
        logger.info(get_string("kb_index_done").format(count))

    if ask:
        answer = asyncio.run(kb.ask(ask))
        _render_answer(kb, answer, kb_format)
