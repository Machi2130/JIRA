import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise ValueError(f"{key} is required but not set in .env")
    return val


@dataclass
class Config:
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_chat_id: int = 0
    poll_interval_minutes: int = 5
    eod_report_hour: int = 18
    eod_report_minute: int = 0
    enable_jira_polling: bool = False
    enable_eod_report: bool = False
    is_jira_cloud: bool = True
    default_project_key: str = "STO"
    groq_api_key: str = ""
    webhook_port: int = 8080
    claude_work_dir: str = ""

    def __post_init__(self):
        self.jira_base_url = _require("JIRA_BASE_URL").rstrip("/")
        self.jira_email = _require("JIRA_EMAIL")
        self.jira_api_token = _require("JIRA_API_TOKEN")
        self.telegram_bot_token = _require("TELEGRAM_BOT_TOKEN")
        self.telegram_allowed_chat_id = int(_require("TELEGRAM_ALLOWED_CHAT_ID"))
        self.poll_interval_minutes = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))
        self.eod_report_hour = int(os.getenv("EOD_REPORT_HOUR", "18"))
        self.eod_report_minute = int(os.getenv("EOD_REPORT_MINUTE", "0"))
        self.enable_jira_polling = os.getenv("ENABLE_JIRA_POLLING", "0").strip().lower() in ("1", "true", "yes", "on")
        self.enable_eod_report = os.getenv("ENABLE_EOD_REPORT", "0").strip().lower() in ("1", "true", "yes", "on")
        self.is_jira_cloud = "atlassian.net" in self.jira_base_url
        self.default_project_key = os.getenv("DEFAULT_PROJECT_KEY", "STO").upper()
        self.groq_api_key = os.getenv("GROQ_API_KEY") or self.groq_api_key
        self.webhook_port = int(os.getenv("WEBHOOK_PORT", "8080"))
        self.claude_work_dir = os.getenv("CLAUDE_WORK_DIR", str(Path.home()))
