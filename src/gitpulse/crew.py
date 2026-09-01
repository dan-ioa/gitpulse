import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

from crewai import Agent, Crew, LLM, Process, Task
from crewai.project import CrewBase, agent, crew, task
from crewai_tools import GithubSearchTool, SerperDevTool

from gitpulse.tools.custom_tool import SendHtmlEmailTool

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local").lower()
MODEL = os.getenv("MODEL") or os.getenv("OPENAI_MODEL_NAME")
BASE_URL = os.getenv("BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
API_KEY = os.getenv("LM_API_TOKEN") or os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


def is_local_base_url(url: str | None) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    return parsed.hostname in {"127.0.0.1", "localhost"}


@lru_cache(maxsize=1)
def build_llm() -> LLM:
    """Build the configured LLM once and reuse it across all agents."""
    if LLM_PROVIDER == "google":
        model = MODEL or "gemini/gemini-2.0-flash"
        if not GOOGLE_API_KEY:
            raise ValueError("Missing GOOGLE_API_KEY in .env for LLM_PROVIDER=google")
        return LLM(model=model, api_key=GOOGLE_API_KEY)

    # local provider (LM Studio)
    if not MODEL:
        raise ValueError("Missing MODEL or OPENAI_MODEL_NAME in .env")

    api_key = API_KEY
    if not api_key and is_local_base_url(BASE_URL):
        # LM Studio's OpenAI-compatible endpoint can run without auth.
        api_key = "lm-studio"
    if not api_key:
        raise ValueError("Missing LM_API_TOKEN or OPENAI_API_KEY in .env")

    llm_kwargs = {
        "model": MODEL,
        "api_key": api_key,
    }
    if BASE_URL:
        llm_kwargs["base_url"] = BASE_URL
    if is_local_base_url(BASE_URL):
        # Force LM Studio through the OpenAI-compatible provider path.
        llm_kwargs["provider"] = "openai"

    return LLM(**llm_kwargs)

@CrewBase
class GitPulseCrew:
    """The GitPulse crew."""

    def _research_tools(self) -> list:
        tools = [SerperDevTool()]
        if GITHUB_TOKEN:
            tools.append(GithubSearchTool(
                gh_token=GITHUB_TOKEN,
                content_types=["repositories"],
            ))
        return tools

    @agent
    def daily_trend_researcher(self) -> Agent:
        return Agent(
            config=self.agents_config["daily_trend_researcher"],
            tools=self._research_tools(),
            reasoning=False,
            max_reasoning_attempts=None,
            inject_date=True,
            allow_delegation=False,
            max_iter=25,
            max_rpm=None,
            max_execution_time=None,
            llm=build_llm(),
        )

    @agent
    def content_creator_and_summarizer(self) -> Agent:
        return Agent(
            config=self.agents_config["content_creator_and_summarizer"],
            tools=[],
            reasoning=False,
            max_reasoning_attempts=None,
            inject_date=True,
            allow_delegation=False,
            max_iter=25,
            max_rpm=None,
            max_execution_time=None,
            llm=build_llm(),
        )

    @agent
    def email_reporter(self) -> Agent:
        return Agent(
            config=self.agents_config["email_reporter"],
            tools=[SendHtmlEmailTool()],
            reasoning=False,
            max_reasoning_attempts=None,
            inject_date=True,
            allow_delegation=False,
            max_iter=25,
            max_rpm=None,
            max_execution_time=None,
            llm=build_llm(),
        )

    @task
    def research_daily_trends(self) -> Task:
        return Task(
            config=self.tasks_config["research_daily_trends"],
            markdown=False,
        )

    @task
    def create_headlines_and_summaries(self) -> Task:
        return Task(
            config=self.tasks_config["create_headlines_and_summaries"],
            markdown=False,
        )

    @task
    def send_daily_trends_email_report(self) -> Task:
        return Task(
            config=self.tasks_config["send_daily_trends_email_report"],
            markdown=True,
        )

    @crew
    def crew(self) -> Crew:
        """Creates the GitPulse crew."""

        return Crew(
            agents=self.agents,  # Automatically created by the @agent decorator
            tasks=self.tasks,  # Automatically created by the @task decorator
            process=Process.sequential,
            verbose=True,
            chat_llm=build_llm(),
        )
