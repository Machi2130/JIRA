import asyncio
import logging
import threading
from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)
_flask_app = Flask(__name__)
_send_notification = None


def _format_webhook_event(payload: dict) -> str | None:
    event = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    key = issue.get("key", "?")
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")

    def esc(t: str) -> str:
        for ch in ("_", "*", "`", "["):
            t = t.replace(ch, f"\\{ch}")
        return t

    # New comment
    if event == "comment_created":
        comment = payload.get("comment", {})
        author = (comment.get("author") or {}).get("displayName", "?")
        body = comment.get("body", "")
        if isinstance(body, dict):
            # ADF — extract plain text
            def adf(node):
                if isinstance(node, dict):
                    if node.get("type") == "text":
                        return node.get("text", "")
                    return "".join(adf(c) for c in node.get("content", []))
                return ""
            body = adf(body).strip()
        body = body[:200]
        return (f"*{key}* — new comment by {esc(author)}\n"
                f"_{esc(summary)}_\n\"{esc(body)}\"")

    # Issue updated
    if event == "jira:issue_updated":
        changelog = payload.get("changelog", {})
        items = changelog.get("items", [])
        lines = []
        for item in items:
            field = item.get("field", "")
            from_str = item.get("fromString") or ""
            to_str = item.get("toString") or ""
            if field == "status":
                lines.append(f"Status: {esc(from_str)} → *{esc(to_str)}*")
            elif field == "assignee":
                lines.append(f"Assignee: {esc(from_str)} → *{esc(to_str)}*")
            elif field == "priority":
                lines.append(f"Priority: {esc(from_str)} → *{esc(to_str)}*")
            elif field == "duedate":
                lines.append(f"Due date: {esc(from_str or 'none')} → *{esc(to_str or 'none')}*")
        if not lines:
            return None  # ignore minor field updates (e.g. rank changes)
        return f"*{key}* updated\n_{esc(summary)}_\n" + "\n".join(lines)

    # New issue created
    if event == "jira:issue_created":
        assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
        status = (fields.get("status") or {}).get("name", "?")
        return (f"New ticket *{key}*\n_{esc(summary)}_\n"
                f"Status: {esc(status)} | Assignee: {esc(assignee)}")

    return None


@_flask_app.route("/jira-webhook", methods=["POST"])
def jira_webhook():
    payload = request.get_json(silent=True) or {}
    event = payload.get("webhookEvent", "unknown")
    logger.info("webhook: received event=%s", event)

    msg = _format_webhook_event(payload)
    if msg and _send_notification:
        try:
            asyncio.run(_send_notification(msg))
            logger.info("webhook: notification sent for %s", event)
        except Exception as e:
            logger.error("webhook: failed to send notification — %s", e)
    else:
        logger.debug("webhook: event=%s ignored (no message generated)", event)

    return jsonify({"ok": True}), 200


@_flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def start_webhook_server(send_notification_fn, port: int = 8080):
    global _send_notification
    _send_notification = send_notification_fn
    logger.info("webhook: starting Flask server on port %d", port)

    def run():
        _flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True, name="webhook-server")
    thread.start()
    logger.info("webhook: server started — listening on http://0.0.0.0:%d/jira-webhook", port)
