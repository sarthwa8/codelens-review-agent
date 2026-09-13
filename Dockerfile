FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

# git: used by the local-git source mode (demo + replay benchmark).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 codelens

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml README.md ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app

COPY --chown=codelens:codelens . .
RUN pip install --no-deps .

USER codelens
# Mounted repos are owned by another uid; git refuses them without this.
RUN git config --global --add safe.directory '*'

# Bake runtime downloads into the image so workers never need network access for them:
# tree-sitter grammars (fetched on first use) and Chroma's default ONNX embedding model.
RUN for attempt in 1 2 3 4 5; do \
      python -c "import tree_sitter_language_pack as t; t.download(['python','javascript','typescript','tsx','go','java','rust','ruby'])" \
      && python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as D; D()(['warm up'])" \
      && break; \
      [ "$attempt" = 5 ] && exit 1; echo "download failed, retrying ($attempt)"; sleep 5; \
    done

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
