"""How a demo video is referenced in a Gemini request.

The uploaded object stays in Cloud Storage. Gemini receives a ``gs://`` URI.
Frames stay at one per second, which is the API default, so a short on-screen
event is still sampled. Audio stays in the file: the API bills audio by
duration, and the rubric asks for spoken explanation.

Explicit context caching is not used for the video. A retry of the same
submission reuses the stored report when the file, model, and prompts match.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from google.genai import types

from app.utils.video_upload import ALLOWED_VIDEO_TYPES


logger = logging.getLogger(__name__)

# generateContent video-understanding page: explicit low is 66 tokens per
# frame; any other resolution is 258. Audio is 32 tokens per second at every
# resolution. The Gemini 3 media-resolution table instead lists low, medium,
# and unspecified video at 70 tokens per frame and audio at 25 per second.
LOW_FRAME_TOKENS = 66
OTHER_FRAME_TOKENS = 258
AUDIO_TOKENS_PER_SECOND = 32

_LEVELS = {
    "low": types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW,
    "medium": types.PartMediaResolutionLevel.MEDIA_RESOLUTION_MEDIUM,
    "high": types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH,
    "unspecified": types.PartMediaResolutionLevel.MEDIA_RESOLUTION_UNSPECIFIED,
}


@dataclass(frozen=True)
class MediaSettings:
    resolution: str
    fps: float


def media_settings() -> MediaSettings:
    """Resolution and frame rate actually sent. FPS never drops below 1."""
    raw_resolution = os.getenv("GEMINI_VIDEO_MEDIA_RESOLUTION", "low").strip().lower()
    if raw_resolution not in _LEVELS:
        logger.warning(
            "GEMINI_VIDEO_MEDIA_RESOLUTION %s is unknown; using low",
            raw_resolution or "empty",
        )
        raw_resolution = "low"
    raw_fps = os.getenv("GEMINI_VIDEO_FPS", "1").strip()
    try:
        fps = float(raw_fps)
    except ValueError:
        logger.warning("GEMINI_VIDEO_FPS %s is not a number; using 1", raw_fps or "empty")
        fps = 1.0
    if fps < 1:
        logger.warning("GEMINI_VIDEO_FPS %s is below 1; using 1.0", fps)
        fps = 1.0
    if fps > 24:
        logger.warning("GEMINI_VIDEO_FPS %s is above 24; using 24", fps)
        fps = 24.0
    return MediaSettings(resolution=raw_resolution, fps=fps)


def validate_video_reference(uri: str, content_type: str | None) -> str:
    """Reject a reference Gemini should not be asked to watch. The object is not read."""
    text = (uri or "").strip()
    rest = text[5:] if text.startswith("gs://") else ""
    bucket, separator, obj = rest.partition("/")
    if not bucket or not separator or not obj:
        raise ValueError("Video must be a gs:// object. The uploaded file was not sent.")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_VIDEO_TYPES:
        raise ValueError(f"Unsupported video type: {mime or 'missing'}")
    return mime


def build_video_part(uri: str, content_type: str | None) -> types.Part:
    """File reference plus sampling settings. Does not download or rewrite the object."""
    mime = validate_video_reference(uri, content_type)
    settings = media_settings()
    part = types.Part.from_uri(
        file_uri=uri.strip(),
        mime_type=mime,
        media_resolution=_LEVELS[settings.resolution],
    )
    part.video_metadata = types.VideoMetadata(fps=settings.fps)
    return part


def estimate_media_tokens(
    duration_seconds: float,
    *,
    resolution: str = "low",
    fps: float = 1.0,
    audio_seconds: float | None = None,
) -> dict[str, float | int | str]:
    """Documented token estimate. ``audio_seconds`` of 0 is a silent track.

    Passing no audio duration means the track is present for the whole clip.
    This does not remove audio from a request.
    """
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be zero or greater")
    if fps <= 0 or fps > 24:
        raise ValueError("fps must be within (0, 24]")
    if resolution not in _LEVELS:
        raise ValueError(f"Unknown resolution: {resolution}")
    heard = duration_seconds if audio_seconds is None else audio_seconds
    if heard < 0 or heard > duration_seconds:
        raise ValueError("audio_seconds must fall inside the clip")
    per_frame = LOW_FRAME_TOKENS if resolution == "low" else OTHER_FRAME_TOKENS
    frames = duration_seconds * fps
    video_tokens = int(round(frames * per_frame))
    audio_tokens = int(round(heard * AUDIO_TOKENS_PER_SECOND))
    return {
        "duration_seconds": duration_seconds,
        "audio_seconds": heard,
        "fps": fps,
        "resolution": resolution,
        "frames": frames,
        "video_tokens": video_tokens,
        "audio_tokens": audio_tokens,
        "media_tokens": video_tokens + audio_tokens,
    }


def build_media_key(
    *,
    video_uri: str,
    content_type: str,
    model: str,
    checklist_template: str,
    video_template: str,
    problem: str,
    solution: str,
    extra_criteria: str,
    max_score: float | None,
) -> str:
    """Identity of one video watch. A different file, model, or rubric misses."""
    settings = media_settings()
    payload = {
        "video_uri": video_uri.strip(),
        "content_type": content_type,
        "model": model,
        "resolution": settings.resolution,
        "fps": settings.fps,
        "checklist_template": checklist_template,
        "video_template": video_template,
        "problem": problem,
        "solution": solution,
        "extra_criteria": extra_criteria,
        "max_score": max_score,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def take_reusable_media(
    stored: Any,
    key: str,
) -> tuple[str, dict[str, Any]] | None:
    """Return the saved checklist and report when this submission already watched this file."""
    if not isinstance(stored, dict) or stored.get("key") != key:
        return None
    checklist = stored.get("checklist")
    video = stored.get("video_result")
    if not isinstance(checklist, str) or not checklist.strip():
        return None
    if not isinstance(video, dict):
        return None
    report = str(video.get("report") or "").strip()
    if not report:
        return None
    return checklist, {
        "report": report,
        "score": video.get("score"),
        "rationale": video.get("rationale"),
    }


def run_cached_video(
    *,
    stored: Any,
    key: str,
    produce: Callable[[], tuple[str, dict[str, Any]]],
    save: Callable[[dict[str, Any]], None],
) -> tuple[tuple[str, dict[str, Any]], bool]:
    """Watch once. A later call with the same key does not send the file again."""
    reused = take_reusable_media(stored, key)
    if reused is not None:
        logger.info("Gemini media reuse key=%s video_tokens=0 audio_tokens=0", key[:12])
        return reused, True
    started = time.perf_counter()
    checklist, video_result = produce()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("Gemini media call key=%s elapsed_ms=%s", key[:12], elapsed_ms)
    payload = {
        "key": key,
        "checklist": checklist,
        "video_result": {
            "report": str(video_result.get("report") or ""),
            "score": video_result.get("score"),
            "rationale": video_result.get("rationale"),
        },
    }
    try:
        save(payload)
    except Exception:
        logger.warning("Gemini media checkpoint was not saved key=%s", key[:12], exc_info=True)
    return (checklist, video_result), False
