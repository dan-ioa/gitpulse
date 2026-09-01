"""Tests for SendHtmlEmailTool's pure helpers and its report-writing branches.

Nothing here touches the network or the Resend API: the email renderer and its
helpers are pure functions, and the send paths are exercised only up to the point
where they bail out on missing configuration.
"""

import re

import pytest

from gitpulse.tools import custom_tool as ct
from gitpulse.tools.custom_tool import (
    ProjectData,
    SendHtmlEmailTool,
    _coerce_project,
    _is_valid_url,
    _lang_colour,
    _og_image_url,
    _render_email,
    _safe_filename,
)


def make_project(name="demo", **overrides):
    defaults = dict(
        name=name,
        github_url=f"https://github.com/owner/{name}",
        owner_repo=f"owner/{name}",
        description="Does a useful thing.",
        language="Python",
        stars="1.2k",
    )
    return ProjectData(**{**defaults, **overrides})


@pytest.fixture
def tool_with_tmp_reports(tmp_path, monkeypatch):
    """SendHtmlEmailTool writing its reports into a temp directory."""
    monkeypatch.setattr(ct, "REPORTS_DIR", tmp_path)
    return SendHtmlEmailTool(), tmp_path


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo",
    "http://example.com",
    "https://example.com/path?q=1",
])
def test_valid_urls_accepted(url):
    assert _is_valid_url(url)


@pytest.mark.parametrize("url", [
    "", "not-a-url", "github.com/owner/repo",   # no scheme
    "ftp://example.com",                        # wrong scheme
    "https://",                                 # no netloc
])
def test_invalid_urls_rejected(url):
    assert not _is_valid_url(url)


# --------------------------------------------------------------------------- #
# Coercion of whatever the agent hands us
# --------------------------------------------------------------------------- #

def test_coerce_passes_through_project_data():
    p = make_project()
    assert _coerce_project(p) is p


def test_coerce_builds_from_dict_and_applies_defaults():
    p = _coerce_project({"name": "x", "github_url": "https://github.com/o/x"})
    assert p is not None
    assert p.owner_repo == ""
    assert p.language == "Unknown"
    assert p.stars == "N/A"


@pytest.mark.parametrize("raw", [
    {"name": "missing url"},   # github_url is required
    {},
    "a string",
    None,
    42,
])
def test_coerce_returns_none_for_unusable_input(raw):
    assert _coerce_project(raw) is None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def test_safe_filename_strips_punctuation():
    assert _safe_filename("Today's Best: AI Tools!") == "Today_s_Best__AI_Tools"


def test_lang_colour_known_and_fallback():
    assert _lang_colour("Python") == "#3572A5"
    assert _lang_colour("Brainfuck") == "#6A737D"


def test_og_image_url_ignores_trailing_slash():
    expected = "https://opengraph.github.com/repo/owner/repo"
    assert _og_image_url("https://github.com/owner/repo") == expected
    assert _og_image_url("https://github.com/owner/repo/") == expected


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_render_fills_every_placeholder():
    html = _render_email([make_project()], "Good morning!")
    # {{{RESEND_UNSUBSCRIBE_URL}}} is a Resend merge tag, resolved server-side at
    # broadcast time, so it is expected to survive local rendering.
    leftover = set(re.findall(r"{{+[A-Z_]+}}+", html)) - {"{{{RESEND_UNSUBSCRIBE_URL}}}"}
    assert leftover == set()


def test_render_includes_welcome_message_and_project_fields():
    html = _render_email([make_project(name="supermemory")], "Happy Tuesday!")
    assert "Happy Tuesday!" in html
    assert "supermemory" in html
    assert "owner/supermemory" in html
    assert "Does a useful thing." in html
    assert "1.2k" in html
    assert _lang_colour("Python") in html


def test_each_project_is_rendered_exactly_once():
    """Regression guard.

    Both templates once carried '<!-- do not remove {{PLACEHOLDER}} -->' comments.
    Because rendering is a plain str.replace, those comment copies were substituted
    too, so every card was emitted twice and the surrounding comment was broken open
    by the card's own '-->'. Keep placeholders out of comments.
    """
    projects = [make_project(name=f"repo{i}") for i in range(4)]
    html = _render_email(projects, "welcome")

    for i in range(4):
        assert html.count(f"owner/repo{i}") == 5, (
            f"repo{i} should appear 5x for a single card "
            f"(1 slug + 3 links + 1 og image), got {html.count(f'owner/repo{i}')}"
        )
    assert html.count("<img") == len(projects)


def test_render_scales_linearly_with_project_count():
    one = len(_render_email([make_project()], "w").encode())
    four = len(_render_email([make_project()] * 4, "w").encode())
    per_card = (four - one) / 3
    # A duplicating renderer roughly doubles this; keep a generous margin.
    assert per_card < 6000, f"card cost {per_card:.0f} bytes — templates may be duplicating"


def test_digest_stays_under_gmail_clipping_limit():
    """Gmail clips messages over ~102KB; the researcher targets up to 12 repos."""
    projects = [make_project(name=f"repo{i}", description="d" * 400) for i in range(12)]
    size = len(_render_email(projects, "w" * 300).encode())
    assert size < 102 * 1024, f"12-project digest is {size / 1024:.1f}KB — Gmail will clip it"


# --------------------------------------------------------------------------- #
# Tool behaviour: always exactly one report, whatever happens
# --------------------------------------------------------------------------- #

def test_invalid_projects_are_filtered_out(tool_with_tmp_reports):
    tool, reports = tool_with_tmp_reports
    result = tool._run(
        subject="Digest",
        projects=[
            {"name": "good", "github_url": "https://github.com/o/good"},
            {"name": "bad", "github_url": "not-a-url"},
            {"name": "unusable"},
        ],
        welcome_message="hi",
    )
    assert "1 skipped" in result or "2 skipped" in result
    body = next(reports.iterdir()).read_text(encoding="utf-8")
    assert "good" in body
    assert "unusable" not in body


def test_no_valid_projects_reports_the_error(tool_with_tmp_reports):
    tool, reports = tool_with_tmp_reports
    result = tool._run(subject="Digest", projects=[{"nope": True}], welcome_message="hi")
    assert "no projects with valid URLs" in result
    assert len(list(reports.iterdir())) == 1


def test_missing_api_key_is_reported_not_raised(tool_with_tmp_reports, monkeypatch):
    tool, reports = tool_with_tmp_reports
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("RESEND_AUDIENCE_ID", "aud_123")

    result = tool._run(subject="Digest", projects=[make_project().model_dump()])
    assert "RESEND_API_KEY is missing" in result
    assert len(list(reports.iterdir())) == 1


def test_missing_audience_id_is_reported_not_raised(tool_with_tmp_reports, monkeypatch):
    tool, reports = tool_with_tmp_reports
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("RESEND_AUDIENCE_ID", raising=False)

    result = tool._run(subject="Digest", projects=[make_project().model_dump()])
    assert "RESEND_AUDIENCE_ID is missing" in result
    assert len(list(reports.iterdir())) == 1


def test_render_failure_is_caught_and_still_writes_one_report(
    tool_with_tmp_reports, monkeypatch
):
    """Rendering happens inside the try block, so a template error is reported."""
    tool, reports = tool_with_tmp_reports
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_AUDIENCE_ID", "aud_123")

    def boom(*_args, **_kwargs):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(ct, "_render_email", boom)

    result = tool._run(subject="Digest", projects=[make_project().model_dump()])
    assert "template exploded" in result
    assert len(list(reports.iterdir())) == 1, "exactly one report per run"


def test_report_records_every_project(tool_with_tmp_reports, monkeypatch):
    tool, reports = tool_with_tmp_reports
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    projects = [make_project(name=f"repo{i}").model_dump() for i in range(3)]
    tool._run(subject="Daily Digest", projects=projects)

    body = next(reports.iterdir()).read_text(encoding="utf-8")
    assert "**Projects included:** 3" in body
    for i in range(3):
        assert f"repo{i}" in body
