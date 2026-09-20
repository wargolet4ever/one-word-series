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


def items_under(parsed: Any, key: str) -> list[Any]:
    """The list a model meant to give you, whatever shape it wrapped it in.

    Asked for `{"issues": [...]}`, a model quite reasonably answers with the
    bare array some of the time. `parsed.get(key)` then raises AttributeError
    on a list — and it raised in the one place that could not afford it, after
    eight paid clips were already on disk.
    """

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        found = parsed.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]
        # Some models use the singular, or wrap it one level deeper.
        for value in parsed.values():
            if isinstance(value, list) and all(isinstance(i, dict) for i in value):
                return value
    return []


def error_detail(exc: Exception) -> str:
    """The platform's own reason, not just the status line.

    `HTTP Error 404: Not Found` is true and useless. Ark puts the reason in the
    response body — "model not found or you have no access to it", "the model
    is not activated" — and urllib hands that body to you exactly once, on the
    exception object. Discarding it is how a run tells you a number and leaves
    you to guess; this cost a real debugging session on the video adapter
    before `vendors.ArkHTTPError` was written, and this is the same fix for the
    other two callers that never got it.
    """

    code = getattr(exc, "code", None)
    label = f"HTTP {code}" if code else type(exc).__name__
    read = getattr(exc, "read", None)
    if not callable(read):
        return f"{label}: {exc}"
    try:
        raw = read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a body we cannot read is not a new failure
        return label
    try:
        body = json.loads(raw)
    except ValueError:
        return f"{label}: {raw.strip()[:300]}" if raw.strip() else label
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        parts = [str(error.get(key, "")).strip() for key in ("code", "message")]
        detail = " ".join(part for part in parts if part)
        if detail:
            return f"{label}: {detail[:300]}"
    return f"{label}: {raw.strip()[:300]}"


def post_chat(payload: dict[str, Any], *, timeout: int = 180) -> dict[str, Any]:
    """One chat/completions call, with the platform's reason kept on failure."""

    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.6,
    timeout: int = 180,
    on_progress=None,
) -> dict[str, Any]:
    """Ask for one JSON object.  Raises ModelUnavailable rather than guessing.

    `on_progress` exists because this call can sit silent for minutes. A whole
    bible is a large JSON object and a reasoning model is slow to produce one;
    with the retries below, a failing call is quiet for the better part of ten
    minutes before it says anything at all. A run that prints nothing for that
    long is indistinguishable from a hung one, and the first thing anyone does
    then is kill it — which is exactly the mistake the video vendor's progress
    line was added to prevent. Same lesson, different call.
    """

    if not configured():
        raise ModelUnavailable("LLM_API_KEY / LLM_MODEL not set")

    def report(**fields: Any) -> None:
        if on_progress:
            on_progress(fields)

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

    started = time.time()
    for attempt in range(MAX_RETRIES + 1):
        report(
            status="waiting", model=model_name(), attempt=attempt + 1,
            of=MAX_RETRIES + 1, elapsed=time.time() - started, timeout=timeout,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            report(status="answered", elapsed=time.time() - started, done=True)
            break
        except Exception as exc:  # noqa: BLE001 — classified, then decided
            if not _retryable(exc) or attempt == MAX_RETRIES:
                report(status="failed", elapsed=time.time() - started, done=True)
                raise ModelUnavailable(
                    f"writing model failed: {error_detail(exc)}"
                ) from exc
            report(
                status="retrying", reason=error_detail(exc)[:120],
                elapsed=time.time() - started,
            )
            time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])

    raw = body["choices"][0]["message"]["content"]
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise ModelUnavailable("writing model did not return a JSON object")
    return parsed
