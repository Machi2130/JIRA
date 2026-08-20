import asyncio
import sys
import logging
from config import Config

# Windows + Python 3.12: ProactorEventLoop breaks httpx/anyio cleanup.
# SelectorEventLoop avoids RuntimeError('Event loop is closed') in Telegram replies.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from jira_client import JiraClient
from jira_poller import JiraPoller
from telegram_bot import JiraTelegramBot
from scheduler import build_scheduler
from webhook_server import start_webhook_server
from ig_handler import register as register_ig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    config = Config()
    logger.info("Config loaded — Jira: %s | Groq enabled: %s | Default project: %s",
                config.jira_base_url, bool(config.groq_api_key), config.default_project_key)

    jira = JiraClient(config)
    logger.info("JiraClient initialized")

    poller = JiraPoller(config, jira)
    bot = JiraTelegramBot(config, jira)
    register_ig(bot._app, config.telegram_allowed_chat_id)

    scheduler = build_scheduler(config, jira, poller, bot.send_notification)
    scheduler.start()
    logger.info("Scheduler started — polling every %d min, EOD report at %d:%02d",
                config.poll_interval_minutes, config.eod_report_hour, config.eod_report_minute)

    start_webhook_server(bot.send_notification, port=config.webhook_port)
    logger.info("Webhook server running on port %d — expose via ngrok or EC2", config.webhook_port)

    logger.info("Bot starting — send /help to your bot on Telegram to begin")
    bot.run_polling()


if __name__ == "__main__":
    main()
