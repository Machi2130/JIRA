#!/usr/bin/env python3
import asyncio
import logging
import os
import threading
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

from config import Config
from jira_client import JiraClient
from jira_poller import JiraPoller
from scheduler import build_scheduler
from telegram_bot import JiraTelegramBot
from webhook_server import _format_webhook_event
from ig_handler import register as register_ig

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
_runtime_lock = threading.Lock()
_runtime_started = False
_runtime_state: dict[str, object] = {}


class _ReportConfig:
    def __init__(self):
        self.jira_base_url = _require("JIRA_BASE_URL").rstrip("/")
        self.jira_email = _require("JIRA_EMAIL")
        self.jira_api_token = _require("JIRA_API_TOKEN")
        self.is_jira_cloud = "atlassian.net" in self.jira_base_url
        self.default_project_key = os.getenv("DEFAULT_PROJECT_KEY", "STO").upper()
        self.eta_field = os.getenv("JIRA_ETA_FIELD", "customfield_11292").strip()
        self.developer_field = os.getenv("JIRA_DEVELOPER_FIELD", "").strip()
        self.qa_field = os.getenv("JIRA_QA_FIELD", "").strip()
        self.devops_field = os.getenv("JIRA_DEVOPS_FIELD", "").strip()


def _require(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required but not set in .env")
    return value


def _ticket_url(base_url: str, key: str) -> str:
    return f"{base_url}/browse/{key}"


def _user_label(value) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        return (
            value.get("displayName")
            or value.get("name")
            or value.get("emailAddress")
            or value.get("accountId")
            or ""
        )
    if isinstance(value, list):
        labels = [_user_label(item) for item in value]
        return ", ".join(label for label in labels if label)
    return str(value)


def _field_value(fields: dict, field_id: str) -> str:
    if not field_id:
        return ""
    value = fields.get(field_id)
    if value is None:
        return ""
    return _user_label(value)


def _comment_text(body) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        node_type = body.get("type")
        if node_type == "text":
            return body.get("text", "")
        parts = [_comment_text(child) for child in body.get("content", [])]
        joiner = "\n" if node_type in {"paragraph", "blockquote", "listItem"} else ""
        return joiner.join(part for part in parts if part)
    if isinstance(body, list):
        return "".join(_comment_text(item) for item in body)
    return ""


def _top_comments(fields: dict) -> list[dict]:
    comment_block = fields.get("comment") or {}
    comments = comment_block.get("comments") or []
    top_five = comments[-5:]
    result = []
    for comment in top_five:
        text = _comment_text(comment.get("body")).strip()
        result.append({
            "author": _user_label(comment.get("author")) or "Unknown",
            "created": comment.get("created", ""),
            "body": text[:300],
        })
    return result


def _extract_today_items(jira: JiraClient, cfg: _ReportConfig) -> dict:
    extra_fields = [cfg.eta_field, cfg.developer_field, cfg.qa_field, cfg.devops_field]
    open_tickets = jira.get_my_report_tickets(extra_fields=extra_fields)
    due_soon = jira.get_due_soon()
    activity = jira.get_activity_today(project_key="")

    def map_ticket(issue: dict) -> dict:
        fields = issue["fields"]
        priority = fields.get("priority") or {}
        assignee = fields.get("assignee") or {}
        return {
            "key": issue["key"],
            "summary": fields.get("summary", ""),
            "status": (fields.get("status") or {}).get("name", "?"),
            "priority": priority.get("name", "?"),
            "due": fields.get("duedate") or "",
            "assignee": assignee.get("displayName", "Unassigned"),
            "developer": _field_value(fields, cfg.developer_field),
            "qa": _field_value(fields, cfg.qa_field),
            "devops": _field_value(fields, cfg.devops_field),
            "eta": _field_value(fields, cfg.eta_field),
            "updated": fields.get("updated", ""),
            "topComments": _top_comments(fields),
            "url": _ticket_url(cfg.jira_base_url, issue["key"]),
        }

    open_items = [map_ticket(issue) for issue in open_tickets]
    due_items = [map_ticket(issue) for issue in due_soon]
    activity_items = [map_ticket(issue) for issue in activity]

    status_counts: dict[str, int] = {}
    for item in open_items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

    return {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "projectKey": cfg.default_project_key,
        "jiraBaseUrl": cfg.jira_base_url,
        "summary": {
            "openTickets": len(open_items),
            "dueSoon": len(due_items),
            "updatedToday": len(activity_items),
        },
        "fieldConfig": {
            "eta": cfg.eta_field,
            "developer": cfg.developer_field,
            "qa": cfg.qa_field,
            "devops": cfg.devops_field,
        },
        "statusCounts": status_counts,
        "openTickets": open_items,
        "dueSoon": due_items,
        "activityToday": activity_items,
    }


def build_report_payload() -> dict:
    cfg = _ReportConfig()
    jira = JiraClient(cfg)
    return _extract_today_items(jira, cfg)


def _run_bot_polling(bot: JiraTelegramBot) -> None:
    logger.info("Telegram bot polling thread starting")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        bot.run_polling()
    finally:
        loop.close()


def ensure_runtime_started() -> None:
    global _runtime_started
    if _runtime_started:
        return
    with _runtime_lock:
        if _runtime_started:
            return

        logger.info("Starting combined runtime for report + Telegram bot")
        config = Config()
        jira = JiraClient(config)
        bot = JiraTelegramBot(config, jira)
        register_ig(bot._app, config.telegram_allowed_chat_id)

        # Previous always-on Jira poller kept below in comments for easy rollback.
        # poller = JiraPoller(config, jira)
        poller = JiraPoller(config, jira) if config.enable_jira_polling else None

        scheduler = build_scheduler(config, jira, poller, bot.send_notification)
        if scheduler.get_jobs():
            scheduler.start()
            logger.info(
                "Scheduler started - Jira polling enabled: %s, EOD enabled: %s",
                config.enable_jira_polling,
                config.enable_eod_report,
            )
        else:
            logger.info("Scheduler not started - running webhook-based Jira updates only")

        bot_thread = threading.Thread(
            target=_run_bot_polling,
            args=(bot,),
            daemon=True,
            name="telegram-bot-polling",
        )
        bot_thread.start()
        logger.info("Telegram bot polling started in background thread")

        _runtime_state["config"] = config
        _runtime_state["jira"] = jira
        _runtime_state["poller"] = poller
        _runtime_state["bot"] = bot
        _runtime_state["scheduler"] = scheduler
        _runtime_state["bot_thread"] = bot_thread
        _runtime_started = True


@app.get("/")
def index():
    ensure_runtime_started()
    return send_from_directory(BASE_DIR, "report.html")


@app.get("/api/report")
def get_report():
    ensure_runtime_started()
    try:
        return jsonify({"ok": True, "report": build_report_payload()})
    except Exception as exc:
        logger.exception("report fetch failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/refresh")
def refresh_report():
    ensure_runtime_started()
    try:
        return jsonify({"ok": True, "report": build_report_payload()})
    except Exception as exc:
        logger.exception("report refresh failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/jira-webhook")
def jira_webhook():
    ensure_runtime_started()
    payload = request.get_json(silent=True) or {}
    event = payload.get("webhookEvent", "unknown")
    logger.info("webhook: received event=%s", event)

    bot = _runtime_state.get("bot")
    msg = _format_webhook_event(payload)
    if msg and bot:
        try:
            # Send immediately when Jira pushes an issue event.
            import asyncio
            asyncio.run(bot.send_notification(msg))
            logger.info("webhook: notification sent for %s", event)
        except Exception as exc:
            logger.error("webhook: failed to send notification - %s", exc)
    else:
        logger.debug("webhook: event=%s ignored (no message generated)", event)

    return jsonify({"ok": True}), 200


@app.get("/health")
def health():
    return jsonify({"status": "ok", "runtime_started": _runtime_started}), 200


@app.get("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(BASE_DIR, filename)


# Previous lightweight http.server version kept for easy rollback.
# class Handler(http.server.SimpleHTTPRequestHandler):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, directory=str(BASE_DIR), **kwargs)
#
#     def _send_json(self, code: int, payload: dict) -> None:
#         body = json.dumps(payload).encode("utf-8")
#         self.send_response(code)
#         self.send_header("Content-Type", "application/json; charset=utf-8")
#         self.send_header("Content-Length", str(len(body)))
#         self.end_headers()
#         self.wfile.write(body)
#
#     def do_GET(self):
#         parsed = urlparse(self.path)
#         if parsed.path == "/api/report":
#             try:
#                 self._send_json(200, {"ok": True, "report": build_report_payload()})
#             except Exception as exc:
#                 logger.exception("report fetch failed")
#                 self._send_json(500, {"ok": False, "error": str(exc)})
#             return
#         if parsed.path == "/":
#             self.path = "/report.html"
#         return super().do_GET()
#
#     def do_POST(self):
#         parsed = urlparse(self.path)
#         if parsed.path == "/api/refresh":
#             try:
#                 self._send_json(200, {"ok": True, "report": build_report_payload()})
#             except Exception as exc:
#                 logger.exception("report refresh failed")
#                 self._send_json(500, {"ok": False, "error": str(exc)})
#             return
#         self.send_response(404)
#         self.end_headers()


if os.getenv("START_EMBEDDED_RUNTIME", "1") == "1":
    ensure_runtime_started()


if __name__ == "__main__":
    ensure_runtime_started()
    port = int(os.getenv("REPORT_PORT", "4173"))
    logger.info("Serving personal Jira report at http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)

# Previous startup kept for easy rollback.
# if __name__ == "__main__":
#     port = int(os.getenv("REPORT_PORT", "4173"))
#     with http.server.ThreadingHTTPServer(("", port), Handler) as httpd:
#         print(f"Serving personal Jira report at http://localhost:{port}")
#         httpd.serve_forever()
