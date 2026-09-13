"""Cache-key construction for review results.

DESIGN NOTE — what belongs in the key is a hit-rate vs. correctness trade-off:

* file content + patch   (spec)   the code actually under review.
* provider, model        (added)  a different model must not serve another model's review.
* prompt_version         (added)  changing the prompt/output format must invalidate old reviews.
* repo_id                (added)  reviews quote retrieved same-repo code; sharing results
                                  across repos could leak private code into another repo's
                                  review. Costs hits across forks — accepted.
* language               (added)  cheap, and changes how the prompt frames the file.

Deliberately EXCLUDED:
* RAG context — it changes as the repo evolves, which would make hits nearly impossible.
  Consequence: a cached review may predate newer similar code elsewhere in the repo.
* file path — a moved file with identical content+patch can reuse the review.
* commit sha / ref — including either would defeat the cache entirely.

Fields are length-prefixed before hashing: naive concatenation is ambiguous
("ab" + "c" and "a" + "bc" hash identically).
"""

import hashlib

# Bump when the set or order of fields below changes.
CACHE_KEY_SCHEMA = "v1"


def _hash_fields(fields: list[str]) -> str:
    digest = hashlib.sha256()
    for field in fields:
        encoded = field.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def build_cache_key(
    *,
    repo_id: int,
    provider: str,
    model: str,
    prompt_version: str,
    language: str | None,
    file_content: str,
    patch: str,
) -> str:
    return _hash_fields(
        [
            CACHE_KEY_SCHEMA,
            str(repo_id),
            provider,
            model,
            prompt_version,
            language or "",
            file_content,
            patch,
        ]
    )


def content_hash(file_content: str, patch: str) -> str:
    """The spec's SHA-256(file content + diff), stored on every audit row."""
    return _hash_fields([file_content, patch])
