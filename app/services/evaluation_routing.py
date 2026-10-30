"""
Choose which Cloud Run worker runs the next video AI analysis.

A superadmin chooses the personal project, the company project, or both.
Shares apply only when both are selected: 4 and 1 sends four videos to the
personal project, then one to the company project, and then repeats.
GitHub AI evaluation does not use this.
"""

from __future__ import annotations

import os

PERSONAL_KEY = "personal"
COMPANY_KEY = "company"
MODE_BOTH = "both"
ROUTING_MODES = (PERSONAL_KEY, COMPANY_KEY, MODE_BOTH)
MAX_SHARE = 20
EVALUATION_ROUTING_DOC_ID = "evaluation_routing"

WORKER_CATALOG: tuple[dict[str, str], ...] = (
    {
        "key": PERSONAL_KEY,
        "name": "Personal project",
        "project_id": "nxt-acad-hackathon",
    },
    {
        "key": COMPANY_KEY,
        "name": "Company project",
        "project_id": "nxt-acad-hackathon-ls-prod",
    },
)


def personal_worker_url() -> str:
    """Tier 1 worker. ``CLOUD_TASKS_TARGET_URL`` remains the existing name."""
    return (
        os.getenv("CLOUD_TASKS_PERSONAL_TARGET_URL") or os.getenv("CLOUD_TASKS_TARGET_URL") or ""
    ).strip()


def company_worker_url() -> str:
    """Company-project worker. Empty means every video stays on the personal worker."""
    return os.getenv("CLOUD_TASKS_COMPANY_TARGET_URL", "").strip()


def worker_urls() -> dict[str, str]:
    urls: dict[str, str] = {}
    personal = personal_worker_url()
    company = company_worker_url()
    if personal:
        urls[PERSONAL_KEY] = personal
    if company:
        urls[COMPANY_KEY] = company
    return urls


def clamp_share(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        number = int(value)
    except ValueError:
        return 0
    return max(0, min(MAX_SHARE, number))


def default_shares(*, company_configured: bool) -> tuple[int, int]:
    """4 personal and 1 company once both workers exist. Otherwise keep today's single worker."""
    if company_configured:
        return 4, 1
    return 1, 0


def shares_from_document(
    doc: dict | None,
    *,
    company_configured: bool,
) -> tuple[int, int]:
    stored = doc or {}
    if "personal_share" not in stored and "company_share" not in stored:
        return default_shares(company_configured=company_configured)
    return clamp_share(stored.get("personal_share")), clamp_share(stored.get("company_share"))


def infer_mode(personal_share: int, company_share: int) -> str | None:
    """Mode implied by shares when a saved document has no explicit choice."""
    if personal_share > 0 and company_share > 0:
        return MODE_BOTH
    if personal_share > 0:
        return PERSONAL_KEY
    if company_share > 0:
        return COMPANY_KEY
    return None


def resolve_mode(doc: dict | None, personal_share: int, company_share: int) -> str:
    """Explicit choice wins. Older documents are read from their shares."""
    stored = (doc or {}).get("mode")
    if stored in ROUTING_MODES:
        return str(stored)
    return infer_mode(personal_share, company_share) or PERSONAL_KEY


def active_sequence(
    mode: str,
    personal_share: int,
    company_share: int,
    *,
    personal_configured: bool,
    company_configured: bool,
) -> list[str]:
    """
    One chosen project receives every video.

    ``both`` keeps the repeating share pattern. A project that is not configured
    is left out so a missing URL cannot be selected at runtime.
    """
    if mode == PERSONAL_KEY and personal_configured:
        return [PERSONAL_KEY]
    if mode == COMPANY_KEY and company_configured:
        return [COMPANY_KEY]
    if mode == MODE_BOTH:
        return build_sequence(
            personal_share,
            company_share,
            personal_configured=personal_configured,
            company_configured=company_configured,
        )
    return build_sequence(
        personal_share,
        company_share,
        personal_configured=personal_configured,
        company_configured=company_configured,
    )


def validate_routing_choice(
    mode: str,
    personal_share: int,
    company_share: int,
    *,
    personal_configured: bool,
    company_configured: bool,
) -> None:
    """Reject a choice the API cannot run. Shares are stored even for one project."""
    if mode == PERSONAL_KEY:
        if not personal_configured:
            raise ValueError("The personal evaluation service is not configured on this API.")
        return
    if mode == COMPANY_KEY:
        if not company_configured:
            raise ValueError("The company evaluation service is not configured on this API.")
        return
    if mode == MODE_BOTH:
        if not personal_configured:
            raise ValueError("The personal evaluation service is not configured on this API.")
        if not company_configured:
            raise ValueError("The company evaluation service is not configured on this API.")
        if personal_share <= 0 or company_share <= 0:
            raise ValueError(
                "Set how many videos each project receives, or choose one project."
            )
        return
    raise ValueError("Choose the personal project, the company project, or both.")


def build_sequence(
    personal_share: int,
    company_share: int,
    *,
    personal_configured: bool,
    company_configured: bool,
) -> list[str]:
    """Workers in the order jobs are handed out. Unconfigured workers are skipped."""
    sequence: list[str] = []
    if personal_configured and personal_share > 0:
        sequence.extend([PERSONAL_KEY] * personal_share)
    if company_configured and company_share > 0:
        sequence.extend([COMPANY_KEY] * company_share)
    return sequence


def routing_summary(sequence: list[str]) -> str:
    if not sequence:
        return "No evaluation service is configured."
    personal_count = sequence.count(PERSONAL_KEY)
    company_count = sequence.count(COMPANY_KEY)
    if company_count == 0:
        return "Every video uses the personal project."
    if personal_count == 0:
        return "Every video uses the company project."
    return (
        f"{_video_phrase(personal_count, 'personal project')} and "
        f"{_video_phrase(company_count, 'company project')}, then the pattern repeats."
    )


def _video_phrase(count: int, name: str) -> str:
    noun = "video" if count == 1 else "videos"
    verb = "uses" if count == 1 else "use"
    return f"{count} {noun} {verb} the {name}"
