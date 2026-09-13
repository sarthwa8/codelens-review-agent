import json
from collections.abc import Iterator

import httpx

from app.llm.adapter import LLMError, LLMEvent, LLMRequest, TextDelta, Usage


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, client: httpx.Client | None = None):
        self.model = model
        self._client = client or httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(600, connect=10)
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMEvent]:
        body = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "options": {"num_predict": request.max_tokens},
        }
        try:
            with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    response.read()
                    raise LLMError(
                        f"ollama {response.status_code}: {response.text[:300]}",
                        retryable=response.status_code >= 500,
                    )
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise LLMError(f"ollama: {event['error']}", retryable=False)
                    content = (event.get("message") or {}).get("content")
                    if content:
                        yield TextDelta(content)
                    if event.get("done"):
                        yield Usage(event.get("prompt_eval_count"), event.get("eval_count"))
                        return
        except httpx.TransportError as exc:
            raise LLMError(f"ollama unreachable: {exc}", retryable=True) from exc
