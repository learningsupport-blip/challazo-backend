"""Gemini prompts for submission video analysis.

Default templates. Admins can override these via the ``ai_evaluation_prompts``
Firestore collection (see ``EvaluationPromptService``). Placeholders must stay
in sync with ``REQUIRED_PLACEHOLDERS`` in ``evaluation_prompt_model``.
"""

CHECKLIST_PROMPT = """From the problem and solution below, write a checklist a reviewer can
verify in the demo video. Use at most 4 numbered sections and at most 4
bullets in each section. One line per bullet. Name each section for this
product. Plain text only: no title, no preamble, no markdown headings.

--- PROBLEM STATEMENT ---
{problem_statement}
--- END PROBLEM STATEMENT ---

--- SOLUTION DESCRIPTION ---
{solution_description}
--- END SOLUTION DESCRIPTION ---
"""


ANALYZE_VIDEO_PROMPT = """Watch the video, including what is said and what is on screen. Compare it
to the CONTEXT. For each context point, decide whether the video covers it,
covers it only in part, or misses it. Do not copy the context back.

--- CONTEXT ---
{context}
--- END CONTEXT ---
"""

# Appended after the analyze_video template so a stored admin prompt cannot
# ask for a second long essay. The stored report is built from this JSON.
VIDEO_OUTPUT_CONTRACT = """
Return JSON only:
{{
  "summary": "<at most 4 sentences>",
  "coverage": [
    {{
      "point": "<context point>",
      "status": "covered|partial|missing",
      "at": "<MM:SS when it appears, or empty>",
      "note": "<at most 20 words>"
    }}
  ],
  "issues": ["<short discrepancy, or an empty list>"],
  "score": <number from 0 to {max_score}>,
  "rationale": "<at most 2 sentences>"
}}
"""


FIELD_SCORE_PROMPT = """Score the student's answer for "{field_label}" from 0 to {max_score}
using the instructions below. If those instructions list sub-metrics, apply
each one and return only the summed score.

Return JSON only:
{{
  "score": <number from 0 to {max_score}>,
  "rationale": "<one or two sentences>"
}}

--- SCORING INSTRUCTIONS ---
{scoring_prompt}
--- END SCORING INSTRUCTIONS ---

--- STUDENT ANSWER ---
{student_answer}
--- END STUDENT ANSWER ---
"""


VIDEO_SCORE_PROMPT = """Score this demo-video report from 0 to {max_score} for clarity, feature
coverage, and alignment with the context already reflected in the report.

Return JSON only:
{{
  "score": <number from 0 to {max_score}>,
  "rationale": "<one or two sentences>"
}}

--- VIDEO ANALYSIS REPORT ---
{video_report}
--- END VIDEO ANALYSIS REPORT ---
"""


GITHUB_EVAL_CONTEXT_PROMPT = """You are a hackathon technical evaluator preparing input for an
automated GitHub repository analyzer.

Read the student's problem statement and solution description, then produce:
1. ``provided_context`` — one paragraph of at most 120 words describing what
   the project is, what it should do, and what the analyzer should verify in the repo.
2. ``rubrics`` — 3–6 short must-have checks as strings (e.g. "Uses an LLM",
   "Has full-stack demo", "Matches stated problem").

Return ONLY valid JSON:
{{
  "provided_context": "<paragraph>",
  "rubrics": ["<check 1>", "<check 2>"]
}}

--- PROBLEM STATEMENT ---
{problem_statement}
--- END PROBLEM STATEMENT ---

--- SOLUTION DESCRIPTION ---
{solution_description}
--- END SOLUTION DESCRIPTION ---
"""


DEFAULT_PROMPT_META = {
    "checklist": {
        "name": "Product & Feature Validation Checklist",
        "description": (
            "Builds a checklist from problem statement + solution description "
            "before video analysis. Placeholders: {problem_statement}, {solution_description}."
        ),
    },
    "analyze_video": {
        "name": "Working Demo Video Analysis",
        "description": (
            "Compares the submitted demo video against the checklist. The model "
            "returns compact JSON; the stored report is markdown built from it. "
            "Placeholder: {context}."
        ),
    },
}


# Tokens admins can embed in scorecard scoring_prompt; filled from the submission
# at evaluation time. Prefer snake_case; Title Case aliases are also accepted.
SCORING_PROMPT_PLACEHOLDERS: list[dict[str, str]] = [
    {
        "token": "{problem_statement}",
        "aliases": "{Problem Statement}",
        "label": "Problem Statement",
        "description": "Student's submitted problem statement text.",
    },
    {
        "token": "{solution_description}",
        "aliases": "{Solution Description}",
        "label": "Solution Description",
        "description": "Student's submitted solution description text.",
    },
    {
        "token": "{theme}",
        "aliases": "{Theme}",
        "label": "Theme",
        "description": (
            "Description of the theme the student selected. Use this on Problem "
            "Statement and Solution Description prompts to score theme relevance."
        ),
    },
    {
        "token": "{theme_name}",
        "aliases": "{Theme Name}",
        "label": "Theme name",
        "description": "Display name of the student's selected theme.",
    },
]

_PROBLEM_PLACEHOLDER_TOKENS = frozenset(
    {
        "{problem_statement}",
        "{Problem Statement}",
        "{PROBLEM_STATEMENT}",
        "{problem}",
    }
)

_PLACEHOLDER_VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "problem_statement": (
        "problem_statement",
        "Problem Statement",
        "PROBLEM_STATEMENT",
        "problem",
    ),
    "solution_description": (
        "solution_description",
        "Solution Description",
        "SOLUTION_DESCRIPTION",
        "solution",
    ),
    "theme": (
        "theme",
        "Theme",
        "THEME",
    ),
    "theme_name": (
        "theme_name",
        "Theme Name",
        "THEME_NAME",
    ),
    "theme_description": (
        "theme_description",
        "Theme Description",
        "THEME_DESCRIPTION",
    ),
}


_THEME_PLACEHOLDER_TOKENS = frozenset(
    {
        "{theme}",
        "{Theme}",
        "{THEME}",
        "{theme_description}",
        "{Theme Description}",
        "{THEME_DESCRIPTION}",
        "{theme_name}",
        "{Theme Name}",
        "{THEME_NAME}",
    }
)


def scoring_placeholder_kind(token: str) -> str | None:
    """Which submission section a rubric placeholder refers to, if it is a known one."""
    name = (token or "").strip()
    wrapped = "{" + name + "}"
    if wrapped in _PROBLEM_PLACEHOLDER_TOKENS:
        return "problem"
    if wrapped in _THEME_PLACEHOLDER_TOKENS:
        return "theme"
    if name in _PLACEHOLDER_VALUE_KEYS["solution_description"]:
        return "solution"
    return None


def scoring_prompt_has_problem_context(template: str) -> bool:
    """True if the admin prompt already references the student's problem statement."""
    text = template or ""
    return any(token in text for token in _PROBLEM_PLACEHOLDER_TOKENS)


def scoring_prompt_has_theme_context(template: str) -> bool:
    """True if the admin prompt already references the student's selected theme."""
    text = template or ""
    return any(token in text for token in _THEME_PLACEHOLDER_TOKENS)


def scoring_prompt_theme_preamble(values: dict[str, str]) -> str:
    """Block prepended to PS/SD prompts that do not already mention the theme."""
    name = (values.get("theme_name") or "").strip()
    description = (values.get("theme_description") or values.get("theme") or "").strip()
    if not name and not description:
        return ""
    if name and description and description != name:
        body = f"{name}\n\n{description}"
    else:
        body = description or name
    return f"--- SELECTED THEME ---\n{body}\n--- END SELECTED THEME ---\n\n"


def interpolate_scoring_prompt(template: str, values: dict[str, str]) -> str:
    """
    Replace ``{problem_statement}`` / ``{Problem Statement}``, solution, and
    ``{theme}`` / ``{Theme}`` tokens with submission values. Unknown ``{tokens}``
    are left unchanged. Does not use ``str.format`` so JSON braces in prompts
    are safe.
    """
    result = template or ""
    for canonical, aliases in _PLACEHOLDER_VALUE_KEYS.items():
        value = values.get(canonical) or ""
        for alias in aliases:
            result = result.replace("{" + alias + "}", value)
    # Any extra submission field keys passed in values.
    for key, value in values.items():
        if key in _PLACEHOLDER_VALUE_KEYS:
            continue
        result = result.replace("{" + key + "}", value or "")
        title = key.replace("_", " ").title()
        result = result.replace("{" + title + "}", value or "")
    return result
