"""OpenAI-compatible JSON caller, reusing the repo's existing env contract.

Same three variables as `analyzer.py` / `orchestrator.py`:

    LLM_API_KEY, LLM_MODEL, LLM_BASE_URL (default https://api.openai.com/v1)

Volcengine Ark speaks this protocol too, so one Ark key can drive both the
writing model here and the Seedance video model in `vendors.py`:

    LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

MAX_RETRIES = 2
RETRY_BACKOFF_S = (1.0, 3.0)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelUnavailable(RuntimeError):
    """No key configured, or the call failed after its retries."""


def configured() -> bool:
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


def model_name() -> str:
    return os.getenv("LLM_MODEL", "")


def _retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in RETRYABLE_STATUS
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _extract_json(raw: str) -> Any:
    raw = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (raw.find("{"), raw.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ValueError("model returned no JSON")
    closer = "}" if raw[start] == "{" else "]"
    end = raw.rfind(closer)
    if end <= start:
        raise ValueError("model returned truncated JSON")
    return json.loads(raw[start : end + 1])


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.6,
    timeout: int = 180,
) -> dict[str, Any]:
    """Ask for one JSON object.  Raises ModelUnavailable rather than guessing."""

    if not configured():
        raise ModelUnavailable("LLM_API_KEY / LLM_MODEL not set")

    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": os.environ["LLM_MODEL"],
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001 — classified, then decided
            if not _retryable(exc) or attempt == MAX_RETRIES:
                code = getattr(exc, "code", None)
                label = f"HTTP {code}" if code else type(exc).__name__
                raise ModelUnavailable(f"writing model failed: {label}") from exc
            time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])

    raw = body["choices"][0]["message"]["content"]
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise ModelUnavailable("writing model did not return a JSON object")
    return parsed
