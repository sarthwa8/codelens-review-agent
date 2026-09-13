"""Turn a file's changed chunks into a deduplicated, ranked set of similar same-repo snippets."""

import logging

from app.parsing.treesitter import CodeChunk
from app.rag.chroma_store import CodeIndex, RetrievedSnippet

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(self, index: CodeIndex, *, top_k: int, max_distance: float, max_queries: int):
        self.index = index
        self.top_k = top_k
        self.max_distance = max_distance
        self.max_queries = max_queries

    def similar_code(
        self, repo_id: int, path: str, changed: list[CodeChunk]
    ) -> list[RetrievedSnippet]:
        best: dict[str, RetrievedSnippet] = {}
        # Largest changed units first: they carry the most signal for similarity.
        queries = sorted(changed, key=lambda c: c.end_line - c.start_line, reverse=True)[
            : self.max_queries
        ]
        for chunk in queries:
            try:
                hits = self.index.search(
                    repo_id, chunk, k=self.top_k, max_distance=self.max_distance, exclude_path=path
                )
            except Exception:
                # RAG is an enhancement: an unavailable vector store must not fail the review.
                logger.warning("similarity search failed for %s", chunk.location, exc_info=True)
                return []
            for hit in hits:
                if hit.location not in best or hit.distance < best[hit.location].distance:
                    best[hit.location] = hit
        return sorted(best.values(), key=lambda s: s.distance)
