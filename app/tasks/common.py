import random


def backoff(retries: int, exc: Exception) -> float:
    """Seconds to wait before retrying: the provider's retry-after hint, else jittered exponential."""
    hint = getattr(exc, "retry_after", None)
    if hint:
        return float(hint)
    return min(300.0, 5 * 2**retries) + random.uniform(0, 3)  # jitter avoids synchronized retries
