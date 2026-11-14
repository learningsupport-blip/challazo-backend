"""Output caps, truncation handling, and compact video JSON."""

from types import SimpleNamespace

import pytest

from app.services.gemini_output import field_score_task, generate_content, max_output_tokens
from app.services.submission.analysis import AnalysisMixin


def _response(text: str, finish: str = "STOP", output_tokens: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish)],
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=output_tokens,
            thoughts_token_count=0,
            cached_content_token_count=0,
        ),
    )


class _Models:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_output_limits_follow_task_and_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_MAX_OUTPUT_TOKENS_VIDEO", raising=False)
    monkeypatch.delenv("GEMINI_MAX_OUTPUT_TOKENS_PROBLEM", raising=False)
    monkeypatch.delenv("GEMINI_MAX_OUTPUT_TOKENS_FIELD_SCORE", raising=False)
    assert max_output_tokens("video") == 8192
    assert max_output_tokens("checklist") == 8192
    assert field_score_task("problem_statement") == "problem"
    assert field_score_task("solution") == "solution"
    assert field_score_task("video_explanation") == "field_score"

    monkeypatch.setenv("GEMINI_MAX_OUTPUT_TOKENS_FIELD_SCORE", "900")
    assert max_output_tokens("problem") == 900
    monkeypatch.setenv("GEMINI_MAX_OUTPUT_TOKENS_PROBLEM", "700")
    assert max_output_tokens("problem") == 700
    assert max_output_tokens("solution") == 900


def test_token_cap_is_sent_and_a_cut_off_reply_retries_once():
    models = _Models(
        [
            _response('{"score":', finish="MAX_TOKENS"),
            _response('{"score": 8, "rationale": "Clear problem."}'),
        ]
    )
    client = SimpleNamespace(models=models)

    response = generate_content(
        client,
        model="gemini-3.5-flash",
        contents=["score this"],
        task="problem",
        json_response=True,
    )

    assert response.text.startswith("{")
    assert len(models.calls) == 2
    first = models.calls[0]["config"].max_output_tokens
    second = models.calls[1]["config"].max_output_tokens
    assert first == 4096
    assert second == 8192
    assert models.calls[0]["config"].response_mime_type == "application/json"


def test_a_second_cut_off_does_not_become_a_score():
    models = _Models(
        [
            _response('{"score":', finish="MAX_TOKENS"),
            _response('{"score":', finish="MAX_TOKENS"),
        ]
    )
    client = SimpleNamespace(models=models)

    with pytest.raises(ValueError, match="token limit"):
        generate_content(
            client,
            model="gemini-3.5-flash",
            contents=["score this"],
            task="summary",
            json_response=True,
        )
    assert len(models.calls) == 2


def test_incomplete_score_json_raises_instead_of_scoring_zero():
    with pytest.raises(ValueError, match="incomplete"):
        AnalysisMixin._parse_field_score_json('{"score":', max_score=15)
    with pytest.raises(ValueError, match="did not include a score"):
        AnalysisMixin._parse_field_score_json('{"rationale": "nice"}', max_score=15)


def test_video_json_becomes_the_stored_report_and_keeps_the_score():
    raw = """
    {
      "summary": "The student shows a clinic timeline.",
      "coverage": [
        {"point": "Show the pain", "status": "covered", "note": "Opens on the queue"},
        {"point": "Show the fix", "status": "missing", "note": "No save action"}
      ],
      "issues": ["Audio drops out"],
      "score": 12,
      "rationale": "The problem is clear. The save flow is not shown."
    }
    """
    parsed = AnalysisMixin._parse_video_analysis(raw, max_score=20)
    assert parsed is not None
    assert parsed["score"] == 12
    assert "Video Summary" in parsed["report"]
    assert "Show the pain" in parsed["report"]
    assert "✅" in parsed["report"]
    assert "❌" in parsed["report"]
    report, embedded = AnalysisMixin._video_output(parsed)
    assert "clinic timeline" in report
    assert embedded is not None
    row = AnalysisMixin._field_score_from_embedded(
        {"field_key": "video_explanation", "field_label": "Video", "max_score": 20, "weight": 20},
        embedded,
    )
    assert row["score"] == 12
    assert row["max_score"] == 20


def test_video_score_above_the_rubric_is_clamped():
    raw = '{"summary": "Demo", "coverage": [], "issues": [], "score": 99, "rationale": "High"}'
    parsed = AnalysisMixin._parse_video_analysis(raw, max_score=20)
    assert parsed is not None
    assert parsed["score"] == 20


def test_prose_video_reply_does_not_invent_a_score():
    assert AnalysisMixin._parse_video_analysis("## Video Summary\nA long essay.", None) is None
    report, embedded = AnalysisMixin._video_output("## Video Summary\nA long essay.")
    assert embedded is None
    assert "long essay" in report
