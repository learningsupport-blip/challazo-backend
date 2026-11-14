"""Per-task Gemini output limits and one bounded retry when a reply is cut off.

``max_output_tokens`` covers the answer and the model's thinking. A cap that
fits only the answer is returned as ``MAX_TOKENS`` with a cut-off reply.
Defaults leave room for both. A cut-off reply is tried once at the ceiling.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.genai import types

from app.services.evaluation_orchestration import call_with_backoff
from app.services.gemini_budget import commit_call, environment_name, release_call, reserve_call


logger = logging.getLogger(__name__)

# Answer plus thinking. Thinking on gemini-3.5-flash is often around 2k tokens
# and is counted inside this cap.
_DEFAULT_OUTPUT_TOKENS: dict[str, int] = {
    "checklist": 8192,
    "field_score": 4096,
    "problem": 4096,
    "solution": 4096,
    "video": 8192,
    "summary": 2048,
    "github": 4096,
}
_MAX_ALLOWED = 8192


def max_output_tokens(task: str) -> int:
    """Configured cap for one Gemini task. Unknown tasks use the field-score cap."""
    key = (task or "field_score").strip().lower()
    if key not in _DEFAULT_OUTPUT_TOKENS:
        key = "field_score"
    raw = os.getenv(f"GEMINI_MAX_OUTPUT_TOKENS_{key.upper()}", "").strip()
    if not raw and key in ("problem", "solution"):
        raw = os.getenv("GEMINI_MAX_OUTPUT_TOKENS_FIELD_SCORE", "").strip()
    if not raw:
        return _DEFAULT_OUTPUT_TOKENS[key]
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer output limit for %s", key)
        return _DEFAULT_OUTPUT_TOKENS[key]
    if value < 64:
        return 64
    ceiling = _output_ceiling()
    return min(value, ceiling)


def field_score_task(field_key: str) -> str:
    """Problem and solution can use their own caps. Other fields share one cap."""
    key = (field_key or "").strip().lower()
    if key in ("problem_statement", "problem"):
        return "problem"
    if key in ("solution_description", "solution"):
        return "solution"
    return "field_score"


def generate_content(
    client: Any,
    *,
    model: str,
    contents: list[Any],
    task: str,
    json_response: bool = False,
    cached_content: str | None = None,
) -> Any:
    """Call Gemini. A cut-off reply is tried once at the output ceiling.

    Quota and unavailable errors are tried a few times with backoff. Invalid
    JSON and other validation errors are not retried.
    """
    limit = max_output_tokens(task)
    response = call_with_backoff(
        lambda: _generate(
            client,
            model,
            contents,
            limit,
            json_response,
            cached_content=cached_content,
            task=task,
        ),
        task=task,
    )
    if _finish_reason(response) != "MAX_TOKENS":
        _log_usage(task, response, limit)
        return response

    raised = _output_ceiling()
    if raised <= limit:
        _log_usage(task, response, limit)
        raise ValueError(f"Gemini output hit the token limit for {task}")

    logger.warning(
        "Gemini output hit %s tokens for %s; retrying once at %s",
        limit,
        task,
        raised,
    )
    response = call_with_backoff(
        lambda: _generate(
            client,
            model,
            contents,
            raised,
            json_response,
            cached_content=cached_content,
            task=task,
        ),
        task=task,
    )
    _log_usage(task, response, raised)
    if _finish_reason(response) == "MAX_TOKENS":
        raise ValueError(f"Gemini output hit the token limit for {task}")
    return response


def _output_ceiling() -> int:
    raw = os.getenv("GEMINI_MAX_OUTPUT_TOKENS_CEILING", "").strip()
    if not raw:
        return _MAX_ALLOWED
    try:
        value = int(raw)
    except ValueError:
        return _MAX_ALLOWED
    return max(64, min(value, _MAX_ALLOWED))


def _generate(
    client: Any,
    model: str,
    contents: list[Any],
    limit: int,
    json_response: bool,
    cached_content: str | None = None,
    task: str = "field_score",
) -> Any:
    reservation = reserve_call(task, limit)
    try:
        response = _request(client, model, contents, limit, json_response, cached_content)
    except Exception:
        release_call(reservation)
        raise
    commit_call(reservation, _billed_tokens(response))
    return response


def _request(
    client: Any,
    model: str,
    contents: list[Any],
    limit: int,
    json_response: bool,
    cached_content: str | None,
) -> Any:
    config_kwargs: dict[str, Any] = {"max_output_tokens": limit}
    if json_response:
        config_kwargs["response_mime_type"] = "application/json"
    if cached_content:
        config_kwargs["cached_content"] = cached_content
    return client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(**config_kwargs),
    )


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return ""
    value = getattr(reason, "value", None)
    if isinstance(value, str) and value:
        return value
    text = str(reason)
    if "MAX_TOKENS" in text:
        return "MAX_TOKENS"
    return text


def _log_usage(task: str, response: Any, limit: int) -> None:
    usage = getattr(response, "usage_metadata", None)
    modalities = _modality_tokens(usage)
    logger.info(
        "Gemini usage task=%s limit=%s prompt_tokens=%s output_tokens=%s "
        "thoughts_tokens=%s cached_tokens=%s video_tokens=%s audio_tokens=%s "
        "text_tokens=%s finish=%s environment=%s",
        task,
        limit,
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "candidates_token_count", None),
        getattr(usage, "thoughts_token_count", None),
        getattr(usage, "cached_content_token_count", None),
        modalities.get("VIDEO"),
        modalities.get("AUDIO"),
        modalities.get("TEXT"),
        _finish_reason(response),
        environment_name(),
    )


def _billed_tokens(response: Any) -> int:
    usage = getattr(response, "usage_metadata", None)
    total = getattr(usage, "total_token_count", None)
    if total is not None:
        return int(total)
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    output = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
    return prompt + output + thoughts


def _modality_tokens(usage: Any) -> dict[str, int]:
    details = getattr(usage, "prompt_tokens_details", None) or []
    counts: dict[str, int] = {}
    for item in details:
        modality = getattr(item, "modality", None)
        name = getattr(modality, "name", None) or getattr(modality, "value", None) or modality
        key = str(name or "").split(".")[-1].upper()
        count = getattr(item, "token_count", None)
        if key and count is not None:
            counts[key] = int(count)
    return counts
