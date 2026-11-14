"""Stable scoring rules, input dedupe, and explicit cache reuse."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.services.gemini_context import (
    clear_instruction_cache,
    lookup_instruction_cache,
    prepare_field_prompt,
)
from app.services.gemini_output import generate_content
from app.services.submission.analysis import AnalysisMixin
from app.services.submission.prompts import FIELD_SCORE_PROMPT, interpolate_scoring_prompt


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_instruction_cache()
    yield
    clear_instruction_cache()


class _Caches:
    def __init__(self) -> None:
        self.creates: list[tuple] = []
        self.deleted: list[str] = []
        self.fail = False
        self._n = 0
        self._lock = threading.Lock()

    def create(self, *, model, config):
        time.sleep(0.02)
        if self.fail:
            raise RuntimeError("cache create failed")
        with self._lock:
            self._n += 1
            name = f"cachedContents/{self._n}"
            self.creates.append((model, list(config.contents), config.ttl, name))
        return SimpleNamespace(name=name)

    def delete(self, *, name):
        self.deleted.append(name)


class _Models:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("config") and getattr(kwargs["config"], "cached_content", None):
            if self.responses and getattr(self.responses[0], "explode", False):
                self.responses.pop(0)
                raise RuntimeError("cached content expired")
        return self.responses.pop(0)


def _response(text: str = '{"score": 4, "rationale": "Clear."}') -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=0,
            cached_content_token_count=0,
        ),
    )


def _client(caches: _Caches | None = None, responses: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        caches=caches or _Caches(),
        models=_Models(responses or [_response()]),
    )


def test_field_answer_is_sent_once():
    problem = "P" * 2000
    rules = "Score clarity of {problem_statement}. Return a number."
    prepared = prepare_field_prompt(
        field_label="Problem Statement",
        max_score=10,
        scoring_rules=rules,
        student_answer=problem,
        context={"problem_statement": problem},
    )
    old_rules = interpolate_scoring_prompt(rules, {"problem_statement": problem})
    old = FIELD_SCORE_PROMPT.format(
        field_label="Problem Statement",
        max_score=10,
        scoring_prompt=old_rules,
        student_answer=problem,
    )
    assert old.count(problem) == 2
    assert prepared.full_text.count(problem) == 1
    assert problem not in prepared.stable
    assert len(prepared.full_text) < len(old)
    assert "the Problem statement section below" in prepared.stable


def test_solution_gets_problem_once_and_theme_once():
    problem = "Farmers lose crops to pests."
    solution = "S" * 500
    theme = "Agriculture"
    rules = "Compare {solution_description} with the problem. No theme token here."
    prepared = prepare_field_prompt(
        field_label="Solution Description",
        max_score=20,
        scoring_rules=rules,
        student_answer=solution,
        context={
            "problem_statement": problem,
            "solution_description": solution,
            "theme_name": theme,
            "theme_description": "Help farmers.",
        },
        include_problem=True,
        include_theme=True,
    )
    assert prepared.full_text.count(problem) == 1
    assert prepared.full_text.count(solution) == 1
    assert prepared.full_text.count(theme) == 1
    assert solution not in prepared.stable
    assert problem not in prepared.stable


def test_json_example_in_rules_is_left_alone():
    rules = 'Award points.\nExample shape: {"band": "high"}'
    prepared = prepare_field_prompt(
        field_label="Demo",
        max_score=5,
        scoring_rules=rules,
        student_answer="Shown on screen.",
        context={},
    )
    assert '{"band": "high"}' in prepared.stable


def test_short_rules_do_not_create_a_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_CACHE_MIN_TOKENS", raising=False)
    caches = _Caches()
    name = lookup_instruction_cache(
        _client(caches),
        model="gemini-3.5-flash",
        stable_text="Score clarity. " * 20,
    )
    assert name is None
    assert caches.creates == []


def test_cache_off_skips_create(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CONTEXT_CACHE", "off")
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    caches = _Caches()
    name = lookup_instruction_cache(
        _client(caches),
        model="gemini-3.5-flash",
        stable_text="Score clarity in detail. " * 30,
    )
    assert name is None
    assert caches.creates == []


def test_cache_hit_reuses_the_same_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    monkeypatch.setenv("GEMINI_CACHE_TTL_SECONDS", "3600")
    caches = _Caches()
    client = _client(caches)
    stable = "Shared rubric.\n" + ("Apply the scale. " * 40)
    clock = {"t": 1_000.0}
    first = lookup_instruction_cache(
        client, model="gemini-3.5-flash", stable_text=stable, now=lambda: clock["t"]
    )
    second = lookup_instruction_cache(
        client, model="gemini-3.5-flash", stable_text=stable, now=lambda: clock["t"]
    )
    assert first == second == "cachedContents/1"
    assert len(caches.creates) == 1
    assert "Shared rubric." in caches.creates[0][1][0]


def test_expired_cache_is_deleted_and_recreated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    monkeypatch.setenv("GEMINI_CACHE_TTL_SECONDS", "60")
    caches = _Caches()
    client = _client(caches)
    stable = "Rules that are long enough. " * 20
    clock = {"t": 5_000.0}
    first = lookup_instruction_cache(
        client, model="gemini-3.5-flash", stable_text=stable, now=lambda: clock["t"]
    )
    clock["t"] = 5_060.0
    second = lookup_instruction_cache(
        client, model="gemini-3.5-flash", stable_text=stable, now=lambda: clock["t"]
    )
    assert first == "cachedContents/1"
    assert second == "cachedContents/2"
    assert caches.deleted == [first]


def test_rubric_or_model_change_does_not_reuse_the_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    caches = _Caches()
    client = _client(caches)
    first = lookup_instruction_cache(
        client, model="gemini-3.5-flash", stable_text="Rubric version one. " * 20
    )
    changed = lookup_instruction_cache(
        client, model="gemini-3.5-flash", stable_text="Rubric version two. " * 20
    )
    other_model = lookup_instruction_cache(
        client, model="gemini-2.5-flash", stable_text="Rubric version one. " * 20
    )
    assert len({first, changed, other_model}) == 3
    assert len(caches.creates) == 3


def test_create_failure_falls_back_and_a_later_call_can_create(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    caches = _Caches()
    caches.fail = True
    client = _client(caches)
    stable = "Reusable rules. " * 30
    assert lookup_instruction_cache(client, model="gemini-3.5-flash", stable_text=stable) is None
    caches.fail = False
    created = lookup_instruction_cache(client, model="gemini-3.5-flash", stable_text=stable)
    assert created == "cachedContents/1"


def test_concurrent_callers_create_one_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    caches = _Caches()
    client = _client(caches)
    stable = "One shared instruction block. " * 30
    names: list[str | None] = []
    lock = threading.Lock()

    def call() -> None:
        name = lookup_instruction_cache(client, model="gemini-3.5-flash", stable_text=stable)
        with lock:
            names.append(name)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert names == ["cachedContents/1"] * 8
    assert len(caches.creates) == 1


def test_field_score_uses_cache_only_for_rules(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    answer = "The student built a crop alert."
    caches = _Caches()
    client = _client(caches, [_response(), _response()])
    host = AnalysisMixin()
    host.model = "gemini-3.5-flash"
    metric = {
        "field_key": "problem_statement",
        "field_label": "Problem Statement",
        "max_score": 10,
        "scoring_prompt": "Judge {problem_statement} for clarity.",
    }
    host._score_single_field(client, metric, answer, context={"problem_statement": answer})
    host._score_single_field(client, metric, answer, context={"problem_statement": answer})
    assert len(caches.creates) == 1
    assert answer not in caches.creates[0][1][0]
    assert client.models.calls[0]["config"].cached_content == "cachedContents/1"
    assert answer in client.models.calls[0]["contents"][0]
    assert client.models.calls[1]["config"].cached_content == "cachedContents/1"


def test_cached_request_failure_sends_the_full_prompt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    answer = "Q" * 80
    caches = _Caches()
    expired = SimpleNamespace(explode=True)
    client = _client(caches, [expired, _response()])
    host = AnalysisMixin()
    host.model = "gemini-3.5-flash"
    metric = {
        "field_key": "notes",
        "field_label": "Notes",
        "max_score": 5,
        "scoring_prompt": "Score the notes.",
    }
    scored = host._score_single_field(client, metric, answer, context={})
    assert scored["score"] == 4.0
    assert len(client.models.calls) == 2
    assert client.models.calls[1]["config"].cached_content is None
    assert answer in client.models.calls[1]["contents"][0]
    assert caches.deleted == ["cachedContents/1"]


def test_student_text_inside_the_rubric_is_not_cached(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_CACHE_MIN_TOKENS", "1")
    answer = "A" * 80
    caches = _Caches()
    client = _client(caches, [_response()])
    host = AnalysisMixin()
    host.model = "gemini-3.5-flash"
    metric = {
        "field_key": "notes",
        "field_label": "Notes",
        "max_score": 5,
        "scoring_prompt": f"The expected essay is {answer}. Score it.",
    }
    host._score_single_field(client, metric, answer, context={})
    assert caches.creates == []
    assert client.models.calls[0]["config"].cached_content is None
    assert answer in client.models.calls[0]["contents"][0]


def test_generate_content_passes_cached_content_name():
    models = _Models([_response()])
    client = SimpleNamespace(models=models, caches=_Caches())
    generate_content(
        client,
        model="gemini-3.5-flash",
        contents=["only the student answer"],
        task="field_score",
        json_response=True,
        cached_content="cachedContents/abc",
    )
    config = models.calls[0]["config"]
    assert config.cached_content == "cachedContents/abc"
    assert config.response_mime_type == "application/json"
