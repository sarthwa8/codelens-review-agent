"""Embedding providers.

Embeddings are computed here and handed to Chroma as raw vectors, so Chroma never owns (or
persists config for) an embedding function. The provider's ``name`` is part of the collection
name: switching models creates a fresh collection instead of mixing incompatible vectors.
"""

import hashlib
import math
import re
from typing import Protocol

from app.config import Settings


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Dependency-free lexical embedder (feature hashing over identifier sub-tokens).

    Much weaker than a neural model, but deterministic, instant and offline — used in tests and
    as a zero-download fallback. Code search is surprisingly lexical, so it's not useless.
    """

    name = "hashing-512"
    _token = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    _camel = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

    def __init__(self, dimensions: int = 512):
        self.dimensions = dimensions

    def _features(self, text: str) -> list[str]:
        features = []
        for token in self._token.findall(text):
            features.append(token.lower())
            for part in token.split("_"):
                features.extend(p.lower() for p in self._camel.findall(part) if len(p) > 1)
        return features

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


class ChromaDefaultEmbedder:
    """all-MiniLM-L6-v2 via ONNX runtime (bundled with chromadb; model baked into the image)."""

    name = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._fn = DefaultEmbeddingFunction()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._fn(texts)]


class OpenAIEmbedder:
    def __init__(self, api_key: str | None, model: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self.name = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in response.data]


def embedder_name(settings: Settings) -> str:
    """Name without instantiating (the default embedder loads an ONNX model)."""
    return {
        "hashing": HashingEmbedder.name,
        "openai": settings.openai_embedding_model,
        "default": ChromaDefaultEmbedder.name,
    }[settings.embedding_provider]


def get_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings.openai_api_key, settings.openai_embedding_model)
    return ChromaDefaultEmbedder()
