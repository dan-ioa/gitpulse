import os
import re
from datetime import datetime
from pathlib import Path
from typing import Type
from urllib.parse import urlparse

import resend
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
REPORTS_DIR = PROJECT_ROOT / "reports"

LANGUAGE_COLOURS: dict[str, str] = {
    "Python": "#3572A5",
    "JavaScript": "#f1e05a",
    "TypeScript": "#2b7489",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Java": "#b07219",
    "C++": "#f34b7d",
    "C": "#555555",
    "Ruby": "#701516",
    "Swift": "#ffac45",
    "Kotlin": "#A97BFF",
    "Shell": "#89e051",
}


def _is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return result.scheme in ("http", "https") and bool(result.netloc)
    except Exception:
        return False


def _safe_filename(subject: str) -> str:
    return re.sub(r"[^\w\-]", "_", subject).strip("_")


def _lang_colour(lang: str) -> str:
    return LANGUAGE_COLOURS.get(lang, "#6A737D")


def _load_template(filename: str) -> str:
    path = TEMPLATES_DIR / filename
    return path.read_text(encoding="utf-8")


class ProjectData(BaseModel):
    name: str = Field(..., description="Repository name, e.g. 'supermemory'")
    github_url: str = Field(..., description="URL linking to the project, e.g. https://github.com/owner/repo")
    owner_repo: str = Field(default="", description="Owner and repo slug, e.g. 'supermemoryai/supermemory'")
    description: str = Field(default="", description="2-3 sentence description of what the project does and why it matters")
    language: str = Field(default="Unknown", description="Primary programming language")
    stars: str = Field(default="N/A", description="Star count, e.g. '1.2k' or '945'")


def _coerce_project(raw) -> "ProjectData | None":
    if isinstance(raw, ProjectData):
        return raw
    if isinstance(raw, dict):
        try:
            return ProjectData.model_validate(raw)
        except Exception:
            return None
    return None


class SendHtmlEmailInput(BaseModel):
    subject: str = Field(..., description="Email subject line")
    welcome_message: str = Field(
        default="",
        description=(
            "A short, friendly 2-3 sentence personalised intro for the reader. "
            "Mention the date, how many repos were found, and one exciting highlight."
        ),
    )
    projects: list[dict] = Field(
        ...,
        description=(
            "ALL projects to feature in the digest — pass the complete list in a single call. "
            "Do not call this tool more than once. Projects with invalid URLs are filtered automatically."
        ),
    )


def _og_image_url(github_url: str) -> str:
    path = urlparse(github_url).path.rstrip("/")
    return f"https://opengraph.github.com/repo{path}"


def _render_card(p: ProjectData) -> str:
    card = _load_template("project_card.html")
    return (
        card
        .replace("{{PROJECT_NAME}}", p.name)
        .replace("{{PROJECT_URL}}", p.github_url.rstrip("/"))
        .replace("{{OG_IMAGE_URL}}", _og_image_url(p.github_url))
        .replace("{{OWNER_REPO}}", p.owner_repo)
        .replace("{{DESCRIPTION}}", p.description)
        .replace("{{LANGUAGE}}", p.language)
        .replace("{{LANGUAGE_COLOUR}}", _lang_colour(p.language))
        .replace("{{STARS}}", p.stars)
    )


def _render_email(projects: list[ProjectData], welcome_message: str = "") -> str:
    today = datetime.now().strftime("%B %d, %Y")
    cards_html = "".join(_render_card(p) for p in projects)
    email = _load_template("email_template.html")
    return (
        email
        .replace("{{DIGEST_DATE}}", today)
        .replace("{{WELCOME_MESSAGE}}", welcome_message)
        .replace("{{PROJECT_CARDS}}", cards_html)
    )


def _save_report(subject: str, projects: list[ProjectData], email_result: str) -> str:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M-%S")
    filename = f"{_safe_filename(subject)}_{date_str}_{time_str}.md"

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = REPORTS_DIR / filename

    lines = [
        f"# {subject}",
        f"**Date:** {date_str}  **Time:** {time_str}",
        f"**Email result:** {email_result}",
        f"**Projects included:** {len(projects)}",
        "",
        "---",
        "",
    ]
    for p in projects:
        lines += [
            f"## [{p.name}]({p.github_url})",
            f"**Repo:** {p.owner_repo}  |  **Language:** {p.language}  |  **Stars:** {p.stars}",
            "",
            p.description,
            "",
        ]

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath)


class SendHtmlEmailTool(BaseTool):
    name: str = "send_html_email"
    description: str = (
        "Sends the daily GitPulse digest as a Resend Broadcast to all subscribers. "
        "Call this tool ONCE with ALL projects — never once per project. "
        "Always saves a markdown report regardless of email outcome. "
        "The email template is fixed; do not generate HTML yourself."
    )
    args_schema: Type[BaseModel] = SendHtmlEmailInput

    def _run(self, subject: str, projects: list, welcome_message: str = "") -> str:
        api_key = os.getenv("RESEND_API_KEY")
        audience_id = os.getenv("RESEND_AUDIENCE_ID")

        coerced = [_coerce_project(p) for p in projects]
        valid_projects = [p for p in coerced if p is not None and _is_valid_url(p.github_url)]
        skipped = len(projects) - len(valid_projects)

        if not valid_projects:
            result = (
                "Error: no projects with valid URLs could be parsed. "
                "Ensure each project dict has at least 'name' and 'github_url' keys."
            )
            _save_report(subject, [], result)
            return result

        if not api_key:
            result = "Email not sent — RESEND_API_KEY is missing from .env."
            report_path = _save_report(subject, valid_projects, result)
            return f"{result} Report saved to {report_path}"

        if not audience_id:
            result = "Email not sent — RESEND_AUDIENCE_ID is missing from .env."
            report_path = _save_report(subject, valid_projects, result)
            return f"{result} Report saved to {report_path}"

        from_address = os.getenv("RESEND_FROM_EMAIL", "The Git Pulse <onboarding@resend.dev>")

        resend.api_key = api_key

        try:
            html = _render_email(valid_projects, welcome_message)
            broadcast = resend.Broadcasts.create({
                "audience_id": audience_id,
                "from": from_address,
                "subject": subject,
                "html": html,
                "name": f"The Git Pulse {datetime.now().strftime('%Y-%m-%d')}",
            })

            # Extract broadcast ID — SDK may return a dict or typed object
            if isinstance(broadcast, dict):
                if "id" not in broadcast:
                    # API returned an error dict (e.g. unverified domain, bad audience ID)
                    raise ValueError(
                        broadcast.get("message", f"Unexpected API response: {broadcast}")
                    )
                broadcast_id = broadcast["id"]
            else:
                broadcast_id = broadcast.id

            send_result = resend.Broadcasts.send({"broadcast_id": broadcast_id})

            # send_result may also be a dict or object
            if isinstance(send_result, dict) and "id" not in send_result:
                raise ValueError(
                    send_result.get("message", f"Unexpected send response: {send_result}")
                )

            email_result = f"Broadcast sent. Resend broadcast ID: {broadcast_id}"
        except Exception as e:
            email_result = f"Failed to render or send broadcast: {e}"

        report_path = _save_report(subject, valid_projects, email_result)

        summary = f"{email_result} | {len(valid_projects)} projects included"
        if skipped:
            summary += f", {skipped} skipped (missing fields or invalid URL)"
        summary += f". Report saved to {report_path}"
        return summary
