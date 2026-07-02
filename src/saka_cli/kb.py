"""Local knowledge-base harness over synced `pysaka` output.

Wraps `pysaka.knowledge` (registry, aliases, ingest, chunking, store, retrieve,
tools, agent, validator) into a single `KnowledgeBase` object that `saka-cli`'s
`--kb-index` / `--ask` flags drive: scan a group's synced `output/<display>/` tree,
build a searchable index, and answer natural-language questions with cited evidence.

The default backends (`OnnxEmbedder`, `NumpyVectorStore`, `OpenAICompatLLMClient`) are
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

import structlog

from pysaka import Group
from pysaka.client import GROUP_CONFIG
from pysaka.knowledge.agent import KnowledgeAgent
from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.chunking import chunk_documents
from pysaka.knowledge.ingest import ingest_blog, ingest_messages
from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.mentions import MentionDetector
from pysaka.knowledge.models import Answer, Citation, Document, Scope, SourceRef
from pysaka.knowledge.registry import MemberRegistry
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.tools import ToolRunner
from pysaka.knowledge.validator import validate

from saka_cli.llm_backends import OpenAICompatLLMClient
from saka_cli.strings import get_string

logger = structlog.get_logger()

# Gemini's OpenAI-compatibility layer (https://ai.google.dev/gemini-api/docs/openai) -- an
# OpenAICompatLLMClient pointed here needs no Gemini-specific request/response mapping, which
# is what replaced the old hand-rolled `GeminiLLMClient` (native `generateContent` mapping).
GEMINI_OPENAI_COMPAT_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)
DEFAULT_CLOUD_MODEL = "gemini-2.5-flash"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen2.5:14b"
_SNIPPET_LEN = 160


def default_cloud_llm(
    model: str | None = None, api_key: str | None = None
) -> OpenAICompatLLMClient:
    """Build the default cloud `LLMClient`: Gemini via its OpenAI-compatible endpoint.

    `model` defaults to the `SAKA_KB_LLM_MODEL` env var, then `DEFAULT_CLOUD_MODEL`.
    `api_key` defaults to the `GEMINI_API_KEY` env var (may end up `None`, e.g. for tests
    that never actually call `.chat()`).
    """
    return OpenAICompatLLMClient(
        base_url=GEMINI_OPENAI_COMPAT_BASE_URL,
        model=model or os.environ.get("SAKA_KB_LLM_MODEL", DEFAULT_CLOUD_MODEL),
        api_key=api_key or os.environ.get("GEMINI_API_KEY"),
    )


def default_local_llm(
    base_url: str | None = None, model: str | None = None
) -> OpenAICompatLLMClient:
    """Build the default local `LLMClient`: an OpenAI-compatible server (Ollama by default).

    `base_url` defaults to Ollama's local OpenAI-compat endpoint; `model` defaults to
    `DEFAULT_LOCAL_MODEL`. No `api_key` -- local servers (Ollama, llama.cpp) generally don't
    require one.
    """
    return OpenAICompatLLMClient(
        base_url=base_url or DEFAULT_LOCAL_BASE_URL,
        model=model or DEFAULT_LOCAL_MODEL,
        api_key=None,
    )


class KnowledgeBase:
    """Ties `pysaka.knowledge` together for one group's synced `output_dir` tree.

    `embedder` / `vector_store` / `llm` are injectable (tests pass pure-Python fakes);
    left as `None` they are lazily constructed from the real backends (`OnnxEmbedder`
    reading `SAKA_KB_MODEL_DIR`, `NumpyVectorStore`, `default_cloud_llm()` i.e. Gemini via
    its OpenAI-compat endpoint) on first actual use, so building a `KnowledgeBase` never
    requires a model file or network access unless `build_index()`/`ask()` is actually
    called without fakes. To use the local backend instead, pass
    `llm=default_local_llm(...)` (or any other `OpenAICompatLLMClient`) explicitly --
    see `saka_cli.cli`'s `--llm-backend` wiring.
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
            self._llm = default_cloud_llm()
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


def _resolve_llm(
    llm_backend: str | None, llm_base_url: str | None, llm_model: str | None
) -> Any | None:
    """Build the `LLMClient` `run_kb_command` should inject into `KnowledgeBase`, from CLI flags.

    Returns `None` when the user passed none of `--llm-backend`/`--llm-base-url`/`--llm-model`,
    which leaves `KnowledgeBase._ensure_llm()` free to lazily build its own cloud default --
    i.e. unchanged default behavior for callers that never touch these flags. Any explicit flag
    means "the user made a choice": `--llm-backend local` builds `default_local_llm()`
    (optionally overridden by `--llm-base-url`/`--llm-model`); anything else (including just
    `--llm-model` alone, to override the cloud model) builds `default_cloud_llm()`.
    """
    if llm_backend is None and llm_base_url is None and llm_model is None:
        return None
    if llm_backend == "local":
        return default_local_llm(base_url=llm_base_url, model=llm_model)
    return default_cloud_llm(model=llm_model)


def run_kb_command(
    output_dir: Path,
    service: str,
    members_dir: Path,
    ask: str | None,
    kb_index: bool,
    kb_format: str,
    aliases_file: Path | None = None,
    llm_backend: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
) -> None:
    """Entry point wired from `saka_cli.cli.main()`: build the index and/or answer a question.

    `llm_backend`/`llm_base_url`/`llm_model` come straight from the CLI's `--llm-backend`/
    `--llm-base-url`/`--llm-model` flags (all default `None` -- see `_resolve_llm`).
    """
    kb = KnowledgeBase(
        Path(output_dir),
        service,
        members_dir=Path(members_dir),
        aliases_file=aliases_file,
        llm=_resolve_llm(llm_backend, llm_base_url, llm_model),
    )

    if kb_index:
        logger.info(get_string("kb_index_start").format(service))
        count = kb.build_index()
        logger.info(get_string("kb_index_done").format(count))

    if ask:
        if not kb_index:
            logger.warning(get_string("kb_ask_without_index"))
        answer = asyncio.run(kb.ask(ask))
        _render_answer(kb, answer, kb_format)
