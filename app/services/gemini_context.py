"""Separate stable scoring rules from one student's answer.

Explicit context caching on Gemini 3 needs about 4,096 tokens before a cache
is cheaper than sending the text (Gemini Enterprise Agent Platform). These
rubrics are usually smaller, so the default is to send a stable prefix and
then the submission, and to create a cache only when the prefix is large
enough. Student answers, checklists, and videos are never cached.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from google.genai import types

from app.services.submission.prompts import scoring_placeholder_kind


logger = logging.getLogger(__name__)

# Gemini 3 explicit/implicit cache floor on Enterprise. gemini-3.5-flash is
# not given its own number in that table; 4096 is the Gemini 3 floor.
_DEFAULT_MIN_TOKENS = 4096
_DEFAULT_TTL_SECONDS = 3600

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class PreparedPrompt:
    """Stable rules first, this submission's text last."""

    stable: str
    dynamic: str
    estimated_stable_tokens: int

    @property
    def full_text(self) -> str:
        return f"{self.stable.rstrip()}\n\n{self.dynamic.rstrip()}\n"


@dataclass
class _CacheEntry:
    name: str
    expires_at: float
    content_hash: str


_lock = threading.Lock()
_entries: dict[str, _CacheEntry] = {}
_creating: dict[str, threading.Event] = {}


def clear_instruction_cache() -> None:
    """Drop the in-process cache index. Remote caches expire on their own TTL."""
    with _lock:
        _entries.clear()
        _creating.clear()


def estimate_tokens(text: str) -> int:
    """Rough input size. Four characters per token, matching common Gemini counts."""
    return max(1, len(text or "") // 4)


def cache_min_tokens() -> int:
    raw = os.getenv("GEMINI_CACHE_MIN_TOKENS", "").strip()
    if not raw:
        return _DEFAULT_MIN_TOKENS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MIN_TOKENS


def cache_ttl_seconds() -> int:
    raw = os.getenv("GEMINI_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        return max(60, min(int(raw), 86400))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def context_cache_enabled() -> bool:
    flag = os.getenv("GEMINI_CONTEXT_CACHE", "auto").strip().lower()
    return flag not in ("0", "false", "off", "no")


def prepare_field_prompt(
    *,
    field_label: str,
    max_score: float,
    scoring_rules: str,
    student_answer: str,
    context: dict[str, str] | None = None,
    include_problem: bool = False,
    include_theme: bool = False,
) -> PreparedPrompt:
    """Rules stay free of this student's text. The answer is appended once."""
    values = context or {}
    answer = (student_answer or "").strip()
    rules = _rules_without_student_text(scoring_rules or "", values)
    stable = (
        f'Score "{field_label}" from 0 to {max_score:g} using the rules below. '
        "If the rules list sub-metrics, apply each one and return only the summed score.\n\n"
        "Return JSON only:\n"
        '{"score": <number>, "rationale": "<one or two sentences>"}\n\n'
        "--- SCORING RULES ---\n"
        f"{rules}\n"
        "--- END SCORING RULES ---"
    )
    dynamic = _submission_block(
        rules=scoring_rules or "",
        answer=answer,
        field_label=field_label,
        values=values,
        include_problem=include_problem,
        include_theme=include_theme,
    )
    return PreparedPrompt(
        stable=stable,
        dynamic=dynamic,
        estimated_stable_tokens=estimate_tokens(stable),
    )


def drop_instruction_cache(
    *,
    model: str,
    stable_text: str,
    client: Any | None = None,
) -> None:
    """Forget a local cache entry after a cached request fails."""
    stable = (stable_text or "").strip()
    content_hash = hashlib.sha256(f"{model}\n{stable}".encode("utf-8")).hexdigest()
    with _lock:
        entry = _entries.pop(content_hash, None)
    if entry is not None and client is not None:
        _delete_remote(client, entry.name)


def lookup_instruction_cache(
    client: Any,
    *,
    model: str,
    stable_text: str,
    now: Callable[[], float] | None = None,
) -> str | None:
    """Return a cache name for this exact rule text, or None when caching does not pay.

    A miss below the token floor does not call the cache API. A create failure
    returns None so the caller sends the full prompt. Concurrent callers for
    the same text share one create.
    """
    if not context_cache_enabled():
        return None
    stable = (stable_text or "").strip()
    if not stable or not model:
        return None
    estimated = estimate_tokens(stable)
    if estimated < cache_min_tokens():
        logger.info(
            "Gemini cache skip reason=below_minimum tokens_est=%s minimum=%s",
            estimated,
            cache_min_tokens(),
        )
        return None
    content_hash = hashlib.sha256(f"{model}\n{stable}".encode("utf-8")).hexdigest()
    clock = now or time.time
    cached = _fresh_entry(content_hash, clock, client)
    if cached:
        logger.info("Gemini cache hit hash=%s name=%s", content_hash[:12], cached)
        return cached
    return _create_once(client, model, stable, content_hash, clock)


def _rules_without_student_text(rules: str, values: dict[str, str]) -> str:
    """Keep the rubric wording. Point known placeholders at the submission section.

    Brace groups that are not submission fields stay in the rules. A JSON
    example in an admin prompt is shared instructions, not a student's answer.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        section = scoring_placeholder_kind(token)
        if section == "problem":
            return "the Problem statement section below"
        if section == "solution":
            return "the Student answer section below"
        if section == "theme":
            return "the Selected theme section below"
        if _value_for_token(token, values):
            return f"the {token} section below"
        return match.group(0)

    return _PLACEHOLDER.sub(replace, rules)


def _submission_block(
    *,
    rules: str,
    answer: str,
    field_label: str,
    values: dict[str, str],
    include_problem: bool,
    include_theme: bool,
) -> str:
    tokens = {match.group(1).strip() for match in _PLACEHOLDER.finditer(rules)}
    wants_problem = include_problem or any(
        scoring_placeholder_kind(token) == "problem" for token in tokens
    )
    wants_theme = include_theme or any(
        scoring_placeholder_kind(token) == "theme" for token in tokens
    )
    parts: list[str] = ["--- SUBMISSION ---"]
    if wants_theme:
        theme = _theme_text(values)
        if theme:
            parts.append(f"Selected theme:\n{theme}")
    if wants_problem:
        problem = (values.get("problem_statement") or "").strip()
        if problem and problem != answer:
            parts.append(f"Problem statement:\n{problem}")
    wants_solution = any(scoring_placeholder_kind(token) == "solution" for token in tokens)
    if wants_solution:
        solution = (values.get("solution_description") or "").strip()
        if solution and solution != answer:
            parts.append(f"Solution description:\n{solution}")
    for token in sorted(tokens):
        if scoring_placeholder_kind(token) is not None:
            continue
        value = _value_for_token(token, values)
        if not value or value == answer:
            continue
        parts.append(f"{token}:\n{value}")
    parts.append(f"Student answer for {field_label}:\n{answer}")
    return "\n\n".join(parts)


def _value_for_token(token: str, values: dict[str, str]) -> str:
    direct = values.get(token)
    if direct:
        return str(direct).strip()
    for key, value in values.items():
        if str(key).replace("_", " ").title() == token and value:
            return str(value).strip()
    return ""


def _theme_text(values: dict[str, str]) -> str:
    name = (values.get("theme_name") or "").strip()
    description = (values.get("theme_description") or values.get("theme") or "").strip()
    if name and description and description != name:
        return f"{name}\n{description}"
    return description or name


def _fresh_entry(content_hash: str, clock: Callable[[], float], client: Any) -> str | None:
    with _lock:
        entry = _entries.get(content_hash)
        if entry is None:
            return None
        if entry.expires_at > clock():
            return entry.name
        stale = entry.name
        del _entries[content_hash]
    _delete_remote(client, stale)
    return None


def _create_once(
    client: Any,
    model: str,
    stable: str,
    content_hash: str,
    clock: Callable[[], float],
) -> str | None:
    with _lock:
        entry = _entries.get(content_hash)
        if entry and entry.expires_at > clock():
            return entry.name
        event = _creating.get(content_hash)
        if event is None:
            event = threading.Event()
            _creating[content_hash] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait(timeout=30)
        with _lock:
            ready = _entries.get(content_hash)
            if ready and ready.expires_at > clock():
                return ready.name
        return None

    name: str | None = None
    try:
        created = client.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                display_name=f"rubric-{content_hash[:16]}",
                contents=[stable],
                ttl=f"{cache_ttl_seconds()}s",
            ),
        )
        name = str(getattr(created, "name", "") or "").strip() or None
        if name:
            with _lock:
                _entries[content_hash] = _CacheEntry(
                    name=name,
                    expires_at=clock() + cache_ttl_seconds(),
                    content_hash=content_hash,
                )
            logger.info(
                "Gemini cache create hash=%s name=%s ttl_seconds=%s tokens_est=%s",
                content_hash[:12],
                name,
                cache_ttl_seconds(),
                estimate_tokens(stable),
            )
    except Exception:
        logger.warning("Gemini cache create failed hash=%s", content_hash[:12], exc_info=True)
        name = None
    finally:
        with _lock:
            _creating.pop(content_hash, None)
        event.set()
    return name


def _delete_remote(client: Any, name: str) -> None:
    delete = getattr(getattr(client, "caches", None), "delete", None)
    if not callable(delete):
        return
    try:
        delete(name=name)
    except Exception:
        logger.warning("Gemini cache delete failed name=%s", name, exc_info=True)
