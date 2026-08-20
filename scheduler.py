import asyncio
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from config import Config
from jira_client import JiraClient
from jira_poller import JiraPoller
from eod_report import generate_eod_report

logger = logging.getLogger(__name__)


def build_scheduler(
    config: Config,
    jira: JiraClient,
    poller: JiraPoller,
    send_notification,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()

    def poll_job():
        try:
            changes = poller.poll()
            for change in changes:
                from telegram_bot import format_change_notification
                asyncio.run(send_notification(format_change_notification(change)))
        except Exception as e:
            logger.error(f"Poll job error: {e}")

    def eod_job():
        try:
            report = generate_eod_report(
                jira,
                groq_api_key=config.groq_api_key,
                project_key=config.default_project_key,
            )
            asyncio.run(send_notification(f"*EOD Report*\n\n{report}"))
        except Exception as e:
            logger.error(f"EOD report error: {e}")

    if config.enable_jira_polling:
        scheduler.add_job(
            poll_job,
            "interval",
            minutes=config.poll_interval_minutes,
            id="jira_poll",
            next_run_time=datetime.now(),
        )

    if config.enable_eod_report:
        scheduler.add_job(
            eod_job,
            "cron",
            hour=config.eod_report_hour,
            minute=config.eod_report_minute,
            id="eod_report",
        )

    return scheduler
