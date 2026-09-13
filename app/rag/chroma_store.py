"""ChromaDB-backed index of a repository's code chunks."""

import contextlib
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.parsing.treesitter import CodeChunk
from app.rag.embeddings import Embedder

logger = logging.getLogger(__name__)

UPSERT_BATCH = 128


@dataclass(frozen=True)
class RetrievedSnippet:
    path: str
    start_line: int
    end_line: int
    name: str | None
    kind: str
    text: str
    distance: float

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


def _chunk_id(chunk: CodeChunk) -> str:
    raw = f"{chunk.path}\x00{chunk.start_line}\x00{chunk.end_line}\x00{chunk.text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _embed_text(chunk: CodeChunk) -> str:
    # Prefix with location/name so identifiers in the path and symbol contribute to similarity.
    header = f"{chunk.language} {chunk.kind} {chunk.name or ''} {chunk.scope or ''} {chunk.path}"
    return f"{header}\n{chunk.text}"


class CodeIndex:
    """``client_factory`` is called lazily and re-called after a failure, so a worker that
    started while ChromaDB was down recovers without a restart."""

    def __init__(self, client_factory: Callable[[], Any], embedder: Embedder):
        self._client_factory = client_factory
        self._client: Any | None = None
        self._embedder = embedder

    @property
    def embedding_model(self) -> str:
        return self._embedder.name

    def warm_up(self) -> None:
        self._embedder.embed(["warm up"])

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def collection_name(self, repo_id: int) -> str:
        model = re.sub(r"[^a-zA-Z0-9]+", "-", self._embedder.name).strip("-").lower()
        return f"repo-{repo_id}-{model}"[:63]

    def _collection(self, repo_id: int) -> Any:
        try:
            return self._get_client().get_or_create_collection(
                name=self.collection_name(repo_id),
                embedding_function=None,
                configuration={"hnsw": {"space": "cosine"}},
                metadata={"repo_id": repo_id, "embedding_model": self._embedder.name},
            )
        except Exception:
            self._client = None
            raise

    def reset(self, repo_id: int) -> None:
        with contextlib.suppress(Exception):  # collection didn't exist
            self._get_client().delete_collection(self.collection_name(repo_id))

    def replace_file(self, repo_id: int, path: str, chunks: list[CodeChunk]) -> int:
        """Delete-then-add, so chunks for removed/renamed functions don't linger."""
        collection = self._collection(repo_id)
        collection.delete(where={"path": path})
        return self._add(collection, chunks)

    def add_chunks(self, repo_id: int, chunks: list[CodeChunk]) -> int:
        return self._add(self._collection(repo_id), chunks)

    def _add(self, collection: Any, chunks: list[CodeChunk]) -> int:
        unique = {_chunk_id(c): c for c in chunks}
        items = list(unique.items())
        for offset in range(0, len(items), UPSERT_BATCH):
            batch = items[offset : offset + UPSERT_BATCH]
            collection.upsert(
                ids=[chunk_id for chunk_id, _ in batch],
                embeddings=self._embedder.embed([_embed_text(c) for _, c in batch]),
                documents=[c.text for _, c in batch],
                metadatas=[
                    {
                        "path": c.path,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "kind": c.kind,
                        "name": c.name or "",
                        "language": c.language,
                    }
                    for _, c in batch
                ],
            )
        return len(items)

    def delete_paths(self, repo_id: int, paths: list[str]) -> None:
        if paths:
            self._collection(repo_id).delete(where={"path": {"$in": paths}})

    def count(self, repo_id: int) -> int:
        return int(self._collection(repo_id).count())

    def search(
        self,
        repo_id: int,
        query: CodeChunk,
        *,
        k: int,
        max_distance: float,
        exclude_path: str | None,
    ) -> list[RetrievedSnippet]:
        collection = self._collection(repo_id)
        where = {"path": {"$ne": exclude_path}} if exclude_path else None
        result = collection.query(
            query_embeddings=self._embedder.embed([_embed_text(query)]),
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        snippets = []
        for document, metadata, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
        ):
            # Excluding the file itself matters: otherwise the best "similar code" is almost always
            # the previous version of the very function being changed.
            if distance > max_distance:
                continue
            snippets.append(
                RetrievedSnippet(
                    path=str(metadata["path"]),
                    start_line=int(metadata["start_line"]),
                    end_line=int(metadata["end_line"]),
                    name=str(metadata.get("name")) or None,
                    kind=str(metadata.get("kind", "")),
                    text=document,
                    distance=float(distance),
                )
            )
        return snippets


def chroma_client_factory(mode: str, host: str, port: int) -> Callable[[], Any] | None:
    if mode == "disabled":
        return None

    def factory() -> Any:
        import chromadb

        if mode == "ephemeral":
            return chromadb.EphemeralClient()
        return chromadb.HttpClient(host=host, port=port)

    return factory
