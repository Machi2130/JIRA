import asyncio
import logging
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import Config
from jira_client import JiraClient
import nl_handler
import groq_summarizer
import workflow as wf

logger = logging.getLogger(__name__)

_VALID_NL_ACTIONS = {
    "get_ticket", "get_comments", "get_transitions", "get_tickets", "get_due_soon",
    "get_report", "get_workflow", "transition", "assign", "set_priority", "set_due",
    "log_work", "watch", "unwatch", "set_story_points", "search", "add_comment", "dev_task", "unknown",
    "add_label", "remove_label", "link", "create", "get_help",
}


# ── Formatters ────────────────────────────────────────────────────────────────

def format_ticket_list(tickets: list[dict]) -> str:
    if not tickets:
        return "No open tickets found."
    lines = ["*Your open tickets:*\n"]
    for t in tickets:
        f = t["fields"]
        due = f.get("duedate") or "no due date"
        priority = f.get("priority", {})
        priority_name = priority.get("name", "?") if priority else "?"
        lines.append(
            f"• *{t['key']}* — {_md_escape(f['summary'])}\n"
            f"  Status: {_md_escape(f['status']['name'])} | Priority: {_md_escape(priority_name)} | Due: {due}"
        )
    return "\n".join(lines)


def _md_escape(text: str) -> str:
    """Escape characters that break Telegram legacy Markdown."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _adf_to_text(node, depth=0) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    t = node.get("type", "")
    if t == "text":
        return node.get("text", "")
    if t in ("hardBreak", "rule"):
        return "\n"
    children = node.get("content", [])
    text = "".join(_adf_to_text(c) for c in children)
    if t in ("paragraph", "heading", "listItem", "blockquote"):
        return text + "\n"
    if t == "bulletList":
        lines = []
        for item in children:
            lines.append("• " + _adf_to_text(item).strip())
        return "\n".join(lines) + "\n"
    if t == "orderedList":
        lines = []
        for i, item in enumerate(children, 1):
            lines.append(f"{i}. " + _adf_to_text(item).strip())
        return "\n".join(lines) + "\n"
    return text


def _extract_description(fields: dict) -> str:
    desc = fields.get("description")
    if not desc:
        return "No description."
    if isinstance(desc, dict):
        text = _adf_to_text(desc).strip()
        return text[:500] + ("…" if len(text) > 500 else "") if text else "No description."
    if isinstance(desc, str):
        return desc[:500] + ("…" if len(desc) > 500 else "")
    return "No description."


def format_ticket_detail(ticket: dict, comments: list[dict] = None, ai_summary: str = "") -> str:
    key = ticket["key"]
    f = ticket["fields"]
    assignee = f.get("assignee") or {}
    assignee_name = assignee.get("displayName", "Unassigned")
    reporter = (f.get("reporter") or {}).get("displayName", "?")
    labels = ", ".join(f.get("labels", [])) or "none"
    due = f.get("duedate") or "not set"
    priority = f.get("priority") or {}

    # Story points — field name varies by Jira instance
    story_points = (
        f.get("story_points")
        or f.get("customfield_10016")
        or f.get("customfield_10028")
    )
    sp_str = str(story_points) if story_points is not None else "?"

    # Sprint — customfield_10020 is an array of sprint objects
    sprints = f.get("customfield_10020") or []
    sprint_name = sprints[-1].get("name", "?") if sprints else "No sprint"

    description = _extract_description(f)

    lines = [
        f"*{key}* — {_md_escape(f['summary'])}",
        f"Status: {_md_escape(f['status']['name'])} | Priority: {_md_escape(priority.get('name', '?'))} | SP: {sp_str}",
        f"Assignee: {_md_escape(assignee_name)} | Reporter: {_md_escape(reporter)}",
        f"Sprint: {_md_escape(sprint_name)}",
        f"Due: {due} | Labels: {_md_escape(labels)}",
        "",
        f"*Description:*\n{_md_escape(description)}",
    ]

    if ai_summary:
        lines.append("")
        lines.append(f"*AI Summary:*\n{_md_escape(ai_summary)}")

    if comments:
        lines.append("")
        lines.append(f"*Last {min(len(comments), 5)} comments:*")
        for c in comments[-5:]:
            author = _md_escape(c.get("author", {}).get("displayName", "?"))
            body = c.get("body", "")
            if isinstance(body, dict):
                body = _adf_to_text(body).strip()
            body = body[:200] + ("…" if len(body) > 200 else "")
            lines.append(f"\n_{author}:_\n{_md_escape(body)}")

    return "\n".join(lines)


def format_change_notification(change: dict) -> str:
    t = change["type"]
    key = change["key"]
    summary = _md_escape(change["summary"])
    if t == "status_change":
        return (f"*{key}* status changed\n_{summary}_\n"
                f"{_md_escape(change['old'])} → *{_md_escape(change['new'])}*")
    if t == "new_comment":
        return (f"*{key}* — new comment by {_md_escape(change['author'])}\n"
                f"_{summary}_\n\"{_md_escape(change['body'])}\"")
    if t == "new_assignment":
        return (f"*{key}* assigned to *{_md_escape(change.get('assignee', 'someone'))}*\n"
                f"_{summary}_")
    if t == "new_ticket":
        return (f"New ticket *{key}*\n_{summary}_\n"
                f"Status: {_md_escape(change['status'])} | Assignee: {_md_escape(change['assignee'])}")
    if t == "due_soon":
        return f"*{key}* is due soon!\n_{summary}_\nDue: {change.get('due', '?')}"
    return f"Update on *{key}*: {summary}"


def format_transitions(key: str, transitions: list[dict]) -> str:
    names = " | ".join(t["name"] for t in transitions)
    return f"Available statuses for *{key}*:\n{names}"


def format_comments(key: str, comments: list[dict]) -> str:
    if not comments:
        return f"No comments on *{key}*."
    lines = [f"*Comments on {key}:*\n"]
    for c in comments[-5:]:
        body = c.get("body", "")
        if isinstance(body, dict):
            body = _adf_to_text(body).strip()
        author = _md_escape(c["author"]["displayName"])
        lines.append(f"*{author}*:\n{_md_escape(body[:300])}")
    return "\n\n".join(lines)


def parse_comment_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        raise ValueError("Usage: /comment PROJ-123 your comment text here")
    return parts[1].upper(), parts[2]


# ── Bot ───────────────────────────────────────────────────────────────────────

class JiraTelegramBot:
    def __init__(self, config: Config, jira: JiraClient):
        self._cfg = config
        self._jira = jira
        self._last_ticket_by_chat: dict[int, str] = {}
        self._app = (
            Application.builder()
            .token(config.telegram_bot_token)
            .build()
        )
        self._register_handlers()

    def _remember_ticket_context(self, chat_id: int, key: str) -> None:
        if key:
            self._last_ticket_by_chat[chat_id] = key

    def _normalize_nl_command(self, cmd: dict) -> dict:
        if not isinstance(cmd, dict):
            return {"action": "unknown"}

        action = str(cmd.get("action", "unknown")).strip()
        if action not in _VALID_NL_ACTIONS:
            return {"action": "unknown"}

        normalized = {"action": action}
        key = str(cmd.get("key", "") or "").strip().upper()
        if key:
            normalized["key"] = key

        if action == "transition":
            status = str(cmd.get("status", "") or "").strip()
            if not status:
                return {"action": "unknown"}
            normalized["status"] = status
        elif action == "assign":
            name = str(cmd.get("name", "") or "").strip()
            if not name:
                return {"action": "unknown"}
            normalized["name"] = name
        elif action == "set_priority":
            priority = str(cmd.get("priority", "") or "").strip()
            if not priority:
                return {"action": "unknown"}
            normalized["priority"] = priority
        elif action == "set_due":
            date = str(cmd.get("date", "") or "").strip()
            if not date:
                return {"action": "unknown"}
            normalized["date"] = date
        elif action == "log_work":
            time_spent = str(cmd.get("time_spent", "") or "").strip()
            if not time_spent:
                return {"action": "unknown"}
            normalized["time_spent"] = time_spent
        elif action == "set_story_points":
            try:
                normalized["story_points"] = float(cmd.get("story_points"))
            except (TypeError, ValueError):
                return {"action": "unknown"}
        elif action == "add_comment":
            body = str(cmd.get("body", "") or "").strip()
            if not body:
                return {"action": "unknown"}
            normalized["body"] = body
            mentions = cmd.get("mentions")
            if isinstance(mentions, list) and mentions:
                normalized["mentions"] = [str(m).strip() for m in mentions if m]
        elif action == "search":
            query = str(cmd.get("query", "") or "").strip()
            if not query:
                return {"action": "unknown"}
            normalized["query"] = query
        elif action == "create":
            project = str(cmd.get("project", "") or "").strip().upper()
            title = str(cmd.get("title", "") or "").strip()
            if not project or not title:
                return {"action": "unknown"}
            normalized["project"] = project
            normalized["title"] = title
        elif action in {"add_label", "remove_label"}:
            label = str(cmd.get("label", "") or "").strip()
            if not label:
                return {"action": "unknown"}
            normalized["label"] = label
        elif action == "link":
            link_type = str(cmd.get("link_type", "") or "").strip()
            key2 = str(cmd.get("key2", "") or "").strip().upper()
            if not link_type or not key2:
                return {"action": "unknown"}
            normalized["link_type"] = link_type
            normalized["key2"] = key2

        if action in {
            "get_ticket", "get_comments", "get_transitions", "get_workflow",
            "transition", "assign", "set_priority", "set_due", "log_work",
            "watch", "unwatch", "set_story_points", "add_comment",
        } and not normalized.get("key"):
            return {"action": "unknown"}

        return normalized

    async def _parse_with_context(self, chat_id: int, text: str) -> dict:
        parts = text.strip().split(maxsplit=2)
        command = parts[0].lower() if parts else ""
        if command == "help":
            return {"action": "get_help"}
        if command in {"tickets", "due"} and len(parts) == 1:
            text = "show my tickets" if command == "tickets" else "tickets due soon"
        elif command == "ticket" and len(parts) >= 2:
            text = parts[1]
        elif command == "comments" and len(parts) >= 2:
            text = f"show comments for {parts[1]}"
        elif command == "transitions" and len(parts) >= 2:
            text = f"available statuses for {parts[1]}"
        elif command == "status" and len(parts) >= 3:
            text = f"move {parts[1]} to {parts[2]}"
        elif command == "search" and len(parts) >= 2:
            return {"action": "search", "query": parts[1]}
        elif command == "assign" and len(parts) >= 3:
            return {"action": "assign", "key": parts[1].upper(), "name": parts[2]}
        elif command == "priority" and len(parts) >= 3:
            return {"action": "set_priority", "key": parts[1].upper(), "priority": parts[2]}
        elif command == "setdue" and len(parts) >= 3:
            return {"action": "set_due", "key": parts[1].upper(), "date": parts[2]}
        elif command == "log" and len(parts) >= 3:
            return {"action": "log_work", "key": parts[1].upper(), "time_spent": parts[2]}
        elif command in {"watch", "unwatch"} and len(parts) >= 2:
            return {"action": command, "key": parts[1].upper()}
        elif command == "sp" and len(parts) >= 3:
            try:
                points = float(parts[2])
            except ValueError:
                return {"action": "unknown"}
            return {"action": "set_story_points", "key": parts[1].upper(), "story_points": points}
        elif command == "comment" and len(parts) >= 3:
            return {"action": "add_comment", "key": parts[1].upper(), "body": parts[2]}
        elif command == "create" and len(parts) >= 3:
            return {"action": "create", "project": parts[1].upper(), "title": parts[2]}
        elif command == "label" and len(parts) >= 3:
            action = "remove_label" if parts[2].lower() == "remove" else "add_label"
            label_parts = parts[2].split(maxsplit=1)
            if len(label_parts) == 2:
                return {"action": action, "key": parts[1].upper(), "label": label_parts[1]}
        elif command == "link" and len(parts) >= 3:
            link_parts = parts[2].split()
            if len(link_parts) >= 2:
                return {
                    "action": "link",
                    "key": parts[1].upper(),
                    "link_type": " ".join(link_parts[:-1]),
                    "key2": link_parts[-1].upper(),
                }

        cmd = nl_handler.parse(text, self._cfg.default_project_key)
        normalized = self._normalize_nl_command(cmd)
        if normalized.get("action") != "unknown":
            return normalized

        last_key = self._last_ticket_by_chat.get(chat_id)
        if last_key:
            # Keep the original parse path commented for quick rollback/reference.
            # return nl_handler.parse(text, self._cfg.default_project_key)
            contextual_cmd = nl_handler.parse(f"{text} {last_key}", self._cfg.default_project_key)
            contextual_normalized = self._normalize_nl_command(contextual_cmd)
            if (
                contextual_normalized.get("action") != "unknown"
                and not (
                    normalized.get("action") == "unknown"
                    and contextual_normalized.get("action") == "get_ticket"
                )
            ):
                logger.info("NL reused last ticket context: %s", last_key)
                return contextual_normalized

        if self._cfg.groq_api_key:
            groq_cmd = await groq_summarizer.parse_nl_command(
                text,
                self._cfg.default_project_key,
                self._cfg.groq_api_key,
                last_ticket_key=last_key or "",
            )
            groq_normalized = self._normalize_nl_command(groq_cmd)
            if groq_normalized.get("action") != "unknown":
                logger.info("NL parsed by Groq fallback: action=%s", groq_normalized.get("action"))
                return groq_normalized

        return normalized

    def _register_handlers(self):
        add = self._app.add_handler
        add(CommandHandler("tickets", self._cmd_tickets))
        add(CommandHandler("ticket", self._cmd_ticket))
        add(CommandHandler("comments", self._cmd_comments))
        add(CommandHandler("transitions", self._cmd_transitions))
        add(CommandHandler("due", self._cmd_due))
        add(CommandHandler("search", self._cmd_search))
        add(CommandHandler("status", self._cmd_status))
        add(CommandHandler("assign", self._cmd_assign))
        add(CommandHandler("priority", self._cmd_priority))
        add(CommandHandler("setdue", self._cmd_setdue))
        add(CommandHandler("label", self._cmd_label))
        add(CommandHandler("link", self._cmd_link))
        add(CommandHandler("log", self._cmd_log))
        add(CommandHandler("watch", self._cmd_watch))
        add(CommandHandler("unwatch", self._cmd_unwatch))
        add(CommandHandler("comment", self._cmd_comment))
        add(CommandHandler("create", self._cmd_create))
        add(CommandHandler("sp", self._cmd_sp))
        add(CommandHandler("report", self._cmd_report))
        add(CommandHandler("claude", self._cmd_claude))
        add(CommandHandler("help", self._cmd_help))
        add(CommandHandler("start", self._cmd_help))
        add(MessageHandler(filters.TEXT & ~filters.COMMAND, self._cmd_nl))

    async def _reply(self, update: Update, text: str):
        if update.message is None:
            return
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _guard(self, update: Update) -> bool:
        if update.effective_chat.id != self._cfg.telegram_allowed_chat_id:
            await self._reply(update, "Unauthorized.")
            return False
        return True

    # ── View ──────────────────────────────────────────────────────────────────

    async def _cmd_tickets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        try:
            tickets = self._jira.get_my_tickets()
            await self._reply(update, format_ticket_list(tickets))
        except Exception as e:
            await self._reply(update, f"Error fetching tickets: {e}")

    async def _cmd_ticket(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /ticket PROJ-123")
            return
        try:
            ticket = self._jira.get_ticket(ctx.args[0].upper())
            await self._reply(update, format_ticket_detail(ticket))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_comments(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /comments PROJ-123")
            return
        try:
            key = ctx.args[0].upper()
            comments = self._jira.get_comments(key)
            await self._reply(update, format_comments(key, comments))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_transitions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /transitions PROJ-123")
            return
        try:
            key = ctx.args[0].upper()
            transitions = self._jira.get_transitions(key)
            await self._reply(update, format_transitions(key, transitions))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_due(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        try:
            tickets = self._jira.get_due_soon(within_hours=48)
            msg = format_ticket_list(tickets) if tickets else "No tickets due in the next 48 hours."
            await self._reply(update, msg)
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_search(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /search keyword")
            return
        try:
            results = self._jira.search_tickets(" ".join(ctx.args))
            await self._reply(update, format_ticket_list(results))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /status PROJ-123 In Progress")
            return
        key = ctx.args[0].upper()
        new_status = " ".join(ctx.args[1:])
        try:
            self._jira.do_transition(key, new_status)
            await self._reply(update, f"*{key}* moved to *{new_status}*")
        except ValueError as e:
            await self._reply(update, str(e))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_assign(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /assign PROJ-123 John")
            return
        key = ctx.args[0].upper()
        name = " ".join(ctx.args[1:])
        try:
            self._jira.assign_ticket(key, name)
            await self._reply(update, f"*{key}* assigned to *{name}*")
        except ValueError as e:
            await self._reply(update, str(e))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_priority(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /priority PROJ-123 High")
            return
        key = ctx.args[0].upper()
        priority = " ".join(ctx.args[1:])
        try:
            self._jira.set_priority(key, priority)
            await self._reply(update, f"*{key}* priority set to *{priority}*")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_setdue(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /setdue PROJ-123 2026-05-20")
            return
        key = ctx.args[0].upper()
        try:
            self._jira.set_due_date(key, ctx.args[1])
            await self._reply(update, f"*{key}* due date set to *{ctx.args[1]}*")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_label(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 3:
            await self._reply(update, "Usage:\n/label PROJ-123 add backend\n/label PROJ-123 remove backend")
            return
        key = ctx.args[0].upper()
        action = ctx.args[1].lower()
        label = ctx.args[2]
        try:
            if action == "add":
                self._jira.add_label(key, label)
                await self._reply(update, f"Label *{label}* added to *{key}*")
            elif action == "remove":
                self._jira.remove_label(key, label)
                await self._reply(update, f"Label *{label}* removed from *{key}*")
            else:
                await self._reply(update, "Action must be 'add' or 'remove'")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_link(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 3:
            await self._reply(update, "Usage: /link PROJ-123 blocks PROJ-456\nLink types: blocks, relates to, duplicates")
            return
        key1 = ctx.args[0].upper()
        link_type = ctx.args[1]
        key2 = ctx.args[2].upper()
        try:
            self._jira.link_tickets(key1, link_type, key2)
            await self._reply(update, f"*{key1}* {link_type} *{key2}*")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_sp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /sp PROJ-123 5")
            return
        key = ctx.args[0].upper()
        try:
            points = float(ctx.args[1])
            self._jira.set_story_points(key, points)
            await self._reply(update, f"*{key}* story points set to *{int(points)}*")
        except ValueError:
            await self._reply(update, "Story points must be a number.")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_log(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /log PROJ-123 2h")
            return
        key = ctx.args[0].upper()
        time_spent = ctx.args[1]
        try:
            self._jira.log_work(key, time_spent)
            await self._reply(update, f"Logged *{time_spent}* on *{key}*")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /watch PROJ-123")
            return
        key = ctx.args[0].upper()
        try:
            self._jira.watch_ticket(key)
            await self._reply(update, f"Now watching *{key}*")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    async def _cmd_unwatch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /unwatch PROJ-123")
            return
        key = ctx.args[0].upper()
        try:
            self._jira.unwatch_ticket(key)
            await self._reply(update, f"Stopped watching *{key}*")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    # ── Comment (only on explicit request) ────────────────────────────────────

    async def _cmd_comment(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        try:
            key, text = parse_comment_command(update.message.text)
            self._jira.add_comment(key, text)
            await self._reply(update, f"Comment added to *{key}*")
        except ValueError as e:
            await self._reply(update, str(e))
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    # ── Create ────────────────────────────────────────────────────────────────

    async def _cmd_create(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 2:
            await self._reply(update, "Usage: /create PROJECT-KEY Ticket title\nExample: /create PROJ Fix login bug")
            return
        project = ctx.args[0].upper()
        title = " ".join(ctx.args[1:])
        try:
            result = self._jira.create_ticket(project, title)
            await self._reply(update, f"Created *{result['key']}*: {title}")
        except Exception as e:
            await self._reply(update, f"Error: {e}")

    # ── Dev tasks (Claude Code) ───────────────────────────────────────────────

    async def _cmd_claude(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /claude <task description>\nExample: /claude fix the car search bug in STO-54171")
            return
        task = " ".join(ctx.args)
        chat_id = update.effective_chat.id
        asyncio.create_task(self._run_dev_task(chat_id, task))
        await self._reply(update, f"Got it — working on: _{_md_escape(task)}_\nI'll ping you when Claude is done.")

    async def _run_dev_task(self, chat_id: int, task: str):
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", task,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cfg.claude_work_dir,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
            except asyncio.TimeoutError:
                proc.kill()
                await self._app.bot.send_message(chat_id=chat_id, text="Claude timed out after 15 minutes.")
                return

            output = stdout.decode(errors="replace").strip()
            if not output:
                output = stderr.decode(errors="replace").strip() or "Done — no output returned."

            if len(output) > 3800:
                output = output[:3800] + "\n\n...[truncated]"

            await self._app.bot.send_message(chat_id=chat_id, text=output, parse_mode="Markdown")
        except FileNotFoundError:
            await self._app.bot.send_message(chat_id=chat_id, text="Error: `claude` CLI not found. Is Claude Code installed?")
        except Exception as e:
            logger.error("Dev task error: %s", e, exc_info=True)
            await self._app.bot.send_message(chat_id=chat_id, text=f"Error running Claude: {e}")

    # ── Reports ───────────────────────────────────────────────────────────────

    async def _cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await self._reply(update, "Generating EOD report...")
        try:
            from eod_report import generate_eod_report
            report = generate_eod_report(
                self._jira,
                groq_api_key=self._cfg.groq_api_key,
                project_key=self._cfg.default_project_key,
            )
            await self._reply(update, report)
        except Exception as e:
            await self._reply(update, f"Error generating report: {e}")

    # ── Help ──────────────────────────────────────────────────────────────────

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        help_text = (
            "*Jira Bot — Full Command Reference*\n\n"
            "Type commands with or without a slash. Plain text examples:\n"
            "help, tickets, due, ticket STO\\-123, status STO\\-123 In Progress\n\n"
            "*View:*\n"
            "tickets — your open tickets\n"
            "ticket PROJ\\-123 — ticket details\n"
            "comments PROJ\\-123 — view comments\n"
            "transitions PROJ\\-123 — available statuses\n"
            "due — tickets due in 48h\n"
            "search keyword — search tickets\n\n"
            "*Actions:*\n"
            "status PROJ\\-123 In Progress\n"
            "assign PROJ\\-123 John\n"
            "priority PROJ\\-123 High\n"
            "setdue PROJ\\-123 2026\\-05\\-20\n"
            "label PROJ\\-123 add backend\n"
            "label PROJ\\-123 remove backend\n"
            "link PROJ\\-123 blocks PROJ\\-456\n"
            "log PROJ\\-123 2h\n"
            "sp PROJ\\-123 5\n"
            "watch PROJ\\-123\n"
            "unwatch PROJ\\-123\n\n"
            "*Comments \\(only when you ask\\):*\n"
            "comment PROJ\\-123 your text\n"
            "comment PROJ\\-123 @John please review\n\n"
            "*Create:*\n"
            "create PROJ Ticket title\n\n"
            "*Reports:*\n"
            "report — EOD summary now\n\n"
            "*Dev tasks \\(runs Claude Code on your PC\\):*\n"
            "claude fix the bug in STO\\-54171\n"
            "claude implement day 1 of STO\\-53566\n"
            "claude write unit tests for the cart logic"
        )
        await update.message.reply_text(help_text, parse_mode="MarkdownV2")

    # ── Natural language fallback ─────────────────────────────────────────────

    async def _cmd_nl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        text = update.message.text or ""
        chat_id = update.effective_chat.id
        logger.info("NL message received: %r", text)
        cmd = await self._parse_with_context(chat_id, text)
        action = cmd.get("action")
        key = cmd.get("key", "")
        self._remember_ticket_context(chat_id, key)
        logger.info("NL parsed: action=%s key=%s extra=%s", action, key,
                    {k: v for k, v in cmd.items() if k not in ("action", "key")})
        try:
            if action == "get_ticket":
                logger.info("Fetching ticket: %s", key)
                ticket = self._jira.get_ticket(key)
                logger.info("Fetching comments for: %s", key)
                comments = self._jira.get_comments(key)
                logger.info("Got %d comments for %s", len(comments), key)
                ai_summary = ""
                if self._cfg.groq_api_key:
                    logger.info("Groq key present — requesting summary")
                    desc = _extract_description(ticket["fields"])
                    comment_texts = []
                    for c in comments[-5:]:
                        body = c.get("body", "")
                        if isinstance(body, dict):
                            body = _adf_to_text(body).strip()
                        author = c.get("author", {}).get("displayName", "?")
                        comment_texts.append(f"{author}: {body[:300]}")
                    ai_summary = await groq_summarizer.summarize_ticket(desc, comment_texts, self._cfg.groq_api_key)
                else:
                    logger.warning("Groq key not set — skipping AI summary")
                logger.info("Sending ticket detail reply (summary_len=%d)", len(ai_summary))
                await self._reply(update, format_ticket_detail(ticket, comments, ai_summary))
            elif action == "get_comments":
                comments = self._jira.get_comments(key)
                await self._reply(update, format_comments(key, comments))
            elif action == "add_comment":
                body = cmd["body"]
                mentions = cmd.get("mentions", [])
                if mentions:
                    tags = " ".join(f"@{m.split()[0]}" for m in mentions)
                    body = f"{body} {tags}"
                self._jira.add_comment(key, body)
                mention_note = f" (tagged {', '.join(mentions)})" if mentions else ""
                await self._reply(update, f"Comment added to *{key}*{mention_note}")
            elif action in {"add_label", "remove_label"}:
                if action == "add_label":
                    self._jira.add_label(key, cmd["label"])
                    message = f"Label *{cmd['label']}* added to *{key}*"
                else:
                    self._jira.remove_label(key, cmd["label"])
                    message = f"Label *{cmd['label']}* removed from *{key}*"
                await self._reply(update, message)
            elif action == "link":
                self._jira.link_tickets(key, cmd["link_type"], cmd["key2"])
                await self._reply(update, f"*{key}* {cmd['link_type']} *{cmd['key2']}*")
            elif action == "create":
                result = self._jira.create_ticket(cmd["project"], cmd["title"])
                await self._reply(update, f"Created *{result['key']}: {cmd['title']}")
            elif action == "get_transitions":
                transitions = self._jira.get_transitions(key)
                await self._reply(update, format_transitions(key, transitions))
            elif action == "get_tickets":
                tickets = self._jira.get_my_tickets()
                await self._reply(update, format_ticket_list(tickets))
            elif action == "get_due_soon":
                tickets = self._jira.get_due_soon(within_hours=48)
                msg = format_ticket_list(tickets) if tickets else "No tickets due in the next 48 hours."
                await self._reply(update, msg)
            elif action == "search":
                results = self._jira.search_tickets(cmd["query"])
                await self._reply(update, format_ticket_list(results))
            elif action == "get_workflow":
                if key:
                    ticket = self._jira.get_ticket(key)
                    current = ticket["fields"]["status"]["name"]
                    msg = wf.format_workflow(current) + "\n\n" + wf.format_next_steps(current)
                else:
                    msg = wf.format_workflow()
                await self._reply(update, msg)
            elif action == "set_story_points":
                self._jira.set_story_points(key, cmd["story_points"])
                await self._reply(update, f"*{key}* story points set to *{int(cmd['story_points'])}*")
            elif action == "transition":
                self._jira.do_transition(key, cmd["status"])
                await self._reply(update, f"*{key}* moved to *{cmd['status']}*")
                if cmd.get("story_points") is not None:
                    self._jira.set_story_points(key, cmd["story_points"])
                    await self._reply(update, f"*{key}* story points set to *{int(cmd['story_points'])}*")
            elif action == "assign":
                self._jira.assign_ticket(key, cmd["name"])
                await self._reply(update, f"*{key}* assigned to *{cmd['name']}*")
            elif action == "set_priority":
                self._jira.set_priority(key, cmd["priority"])
                await self._reply(update, f"*{key}* priority set to *{cmd['priority']}*")
            elif action == "set_due":
                self._jira.set_due_date(key, cmd["date"])
                await self._reply(update, f"*{key}* due date set to *{cmd['date']}*")
            elif action == "log_work":
                self._jira.log_work(key, cmd["time_spent"])
                await self._reply(update, f"Logged *{cmd['time_spent']}* on *{key}*")
            elif action == "watch":
                self._jira.watch_ticket(key)
                await self._reply(update, f"Now watching *{key}*")
            elif action == "unwatch":
                self._jira.unwatch_ticket(key)
                await self._reply(update, f"Stopped watching *{key}*")
            elif action == "dev_task":
                task = cmd.get("task") or text
                asyncio.create_task(self._run_dev_task(chat_id, task))
                await self._reply(update, f"Got it — working on: _{_md_escape(task)}_\nI'll ping you when Claude is done.")
            elif action == "get_report":
                await self._reply(update, "Generating EOD report...")
                from eod_report import generate_eod_report
                report = generate_eod_report(
                    self._jira,
                    groq_api_key=self._cfg.groq_api_key,
                    project_key=self._cfg.default_project_key,
                )
                await self._reply(update, report)
            elif action == "get_help":
                await self._cmd_help(update, ctx)
            else:
                await self._reply(update, "Didn't understand that. Type help to see available commands.")
        except ValueError as e:
            logger.warning("NL action failed: %s", e)
            msg = str(e)
            # If a transition failed, check if the target is reachable and show the path
            if action == "transition" and key:
                try:
                    ticket = self._jira.get_ticket(key)
                    current = ticket["fields"]["status"]["name"]
                    target = cmd.get("status", "")
                    path = wf.find_path_to(target, current)
                    if path:
                        msg = f"{msg}\n\n{path}"
                    else:
                        msg = f"{msg}\n\n" + wf.format_next_steps(current)
                except Exception:
                    pass
            await self._reply(update, msg)
        except Exception as e:
            logger.error("NL handler error: %s", e, exc_info=True)
            await self._reply(update, f"Error: {e}")

    # ── Outbound notification ─────────────────────────────────────────────────

    async def send_notification(self, text: str):
        await self._app.bot.send_message(
            chat_id=self._cfg.telegram_allowed_chat_id,
            text=text,
            parse_mode="Markdown",
        )

    def run_polling(self):
        # Signal handlers can only be registered from the main interpreter thread.
        self._app.run_polling(stop_signals=None)
