"""Video reference, sampling, silent-audio estimates, and repeat watches."""

from types import SimpleNamespace

import pytest
from google.genai import types

from app.services.gemini_media import (
    LOW_FRAME_TOKENS,
    AUDIO_TOKENS_PER_SECOND,
    OTHER_FRAME_TOKENS,
    build_media_key,
    build_video_part,
    estimate_media_tokens,
    media_settings,
    run_cached_video,
    validate_video_reference,
)
from app.services.submission.analysis import AnalysisMixin


class _Prompts:
    def get_template(self, key: str, hackathon_id: str | None = None) -> str:
        if key == "checklist":
            return "Problem {problem_statement}\nSolution {solution_description}"
        return "Watch the demo.\n{context}"


class _Firebase:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def update_document(self, collection: str, document_id: str, data: dict) -> bool:
        self.saved.append({"collection": collection, "id": document_id, "data": data})
        return True


class _Models:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs["contents"]
        if any(getattr(part, "file_data", None) is not None for part in text):
            body = (
                '{"summary":"Shown","coverage":[{"point":"Login","status":"covered",'
                '"at":"00:04","note":"Form appears"}],"issues":[],"score":8,'
                '"rationale":"The form is visible."}'
            )
        else:
            body = "1. Show the login form"
        return SimpleNamespace(
            text=body,
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                thoughts_token_count=0,
                cached_content_token_count=0,
                prompt_tokens_details=[
                    SimpleNamespace(modality=types.MediaModality.VIDEO, token_count=660),
                    SimpleNamespace(modality=types.MediaModality.AUDIO, token_count=320),
                    SimpleNamespace(modality=types.MediaModality.TEXT, token_count=40),
                ],
            ),
        )


def _host() -> AnalysisMixin:
    host = AnalysisMixin()
    host.model = "gemini-3.5-flash"
    host.evaluation_prompt_service = _Prompts()
    host.firebase = _Firebase()
    host.collection = "submissions"
    return host


def test_short_and_long_clips_scale_with_duration():
    short = estimate_media_tokens(10)
    long = estimate_media_tokens(600)
    assert short["video_tokens"] == 10 * LOW_FRAME_TOKENS
    assert short["audio_tokens"] == 10 * AUDIO_TOKENS_PER_SECOND
    assert short["media_tokens"] == 980
    assert long["video_tokens"] == 600 * LOW_FRAME_TOKENS
    assert long["audio_tokens"] == 600 * AUDIO_TOKENS_PER_SECOND
    assert long["media_tokens"] == short["media_tokens"] * 60


def test_silent_audio_drops_audio_tokens_only():
    heard = estimate_media_tokens(10, audio_seconds=10)
    silent = estimate_media_tokens(10, audio_seconds=0)
    assert silent["video_tokens"] == heard["video_tokens"] == 660
    assert silent["audio_tokens"] == 0
    assert heard["audio_tokens"] == 320


def test_non_low_resolution_uses_the_higher_frame_price():
    costly = estimate_media_tokens(10, resolution="high")
    assert costly["video_tokens"] == 10 * OTHER_FRAME_TOKENS
    assert costly["audio_tokens"] == 320


def test_fps_below_one_second_is_raised(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_VIDEO_FPS", "0.2")
    monkeypatch.setenv("GEMINI_VIDEO_MEDIA_RESOLUTION", "low")
    settings = media_settings()
    assert settings.fps == 1.0
    assert settings.resolution == "low"


def test_invalid_reference_is_rejected_before_a_part_is_built():
    with pytest.raises(ValueError, match="gs://"):
        validate_video_reference("https://example.com/clip.mp4", "video/mp4")
    with pytest.raises(ValueError, match="Unsupported"):
        build_video_part("gs://bucket/clip.mp4", "image/png")
    with pytest.raises(ValueError, match="gs://"):
        build_video_part("", "video/mp4")


def test_part_references_the_same_object_at_one_frame_per_second(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("GEMINI_VIDEO_FPS", raising=False)
    monkeypatch.delenv("GEMINI_VIDEO_MEDIA_RESOLUTION", raising=False)
    uri = "gs://eval-bucket/submissions/student/sub/video.mp4"
    part = build_video_part(uri, "video/mp4")
    assert part.file_data.file_uri == uri
    assert part.file_data.mime_type == "video/mp4"
    assert part.video_metadata.fps == 1.0
    assert part.video_metadata.start_offset is None
    assert part.video_metadata.end_offset is None
    level = part.media_resolution.level
    assert level == types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW
    assert part.media_processing is None


def test_repeat_watch_does_not_call_the_model():
    host = _host()
    client = SimpleNamespace(models=_Models())
    kwargs = dict(
        submission={"media_checkpoint": None},
        submission_id="sub-1",
        collection="submissions",
        problem="Clinics lose the queue.",
        solution="A shared board.",
        video_uri="gs://eval-bucket/submissions/student/sub/video.webm",
        content_type="video/webm",
        extra_criteria=None,
        hackathon_id=None,
        max_score=20,
    )
    checklist, first = host._watch_demo_video(client, **kwargs)
    assert "login form" in checklist.lower() or "Show the login" in checklist
    assert first["score"] == 8
    assert "(00:04)" in first["report"]
    assert len(client.models.calls) == 2
    video_call = client.models.calls[1]
    assert video_call["contents"][0].file_data.file_uri == kwargs["video_uri"]

    stored = host.firebase.saved[-1]["data"]["media_checkpoint"]
    again_kwargs = dict(kwargs)
    again_kwargs["submission"] = {"media_checkpoint": stored}
    checklist_again, second = host._watch_demo_video(client, **again_kwargs)
    assert checklist_again == checklist
    assert second["report"] == first["report"]
    assert len(client.models.calls) == 2


def test_a_different_file_is_watched_again():
    host = _host()
    client = SimpleNamespace(models=_Models())
    common = dict(
        submission_id="sub-1",
        collection="submissions",
        problem="Clinics lose the queue.",
        solution="A shared board.",
        content_type="video/mp4",
        extra_criteria=None,
        hackathon_id=None,
        max_score=20,
    )
    host._watch_demo_video(
        client,
        submission={},
        video_uri="gs://eval-bucket/submissions/a/video.mp4",
        **common,
    )
    stored = host.firebase.saved[-1]["data"]["media_checkpoint"]
    host._watch_demo_video(
        client,
        submission={"media_checkpoint": stored},
        video_uri="gs://eval-bucket/submissions/b/video.mp4",
        **common,
    )
    assert len(client.models.calls) == 4


def test_checkpoint_key_changes_with_the_rubric():
    base = dict(
        video_uri="gs://eval-bucket/clip.mp4",
        content_type="video/mp4",
        model="gemini-3.5-flash",
        checklist_template="one",
        video_template="two",
        problem="p",
        solution="s",
        extra_criteria="",
        max_score=20,
    )
    first = build_media_key(**base)
    changed = build_media_key(**{**base, "video_template": "three"})
    assert first != changed


def test_failed_checkpoint_write_still_returns_the_report():
    def produce():
        return "checklist", {"report": "Seen at 00:01", "score": 5, "rationale": "ok"}

    def save(_payload):
        raise RuntimeError("firestore down")

    pair, reused = run_cached_video(stored=None, key="a" * 64, produce=produce, save=save)
    assert reused is False
    assert pair[1]["score"] == 5
