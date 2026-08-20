import json
import logging
from groq import AsyncGroq

logger = logging.getLogger(__name__)

_client: AsyncGroq | None = None
_client_key: str = ""

_SYSTEM = (
    "You are a concise Jira ticket assistant. "
    "Given a ticket description and recent comments, write a 3-5 line plain-text summary covering:\n"
    "1. What the ticket is about\n"
    "2. The key concern or blocker raised in comments\n"
    "3. The suggested next action\n"
    "Be direct. No bullet points, no markdown, no headers. Plain sentences only."
)

_COMMAND_SYSTEM = (
    "You convert Jira chat messages into strict JSON commands. "
    "Return JSON only, no markdown, no explanation. "
    "Allowed actions: get_ticket, get_comments, get_transitions, get_tickets, "
    "get_due_soon, get_report, get_workflow, transition, assign, set_priority, "
    "set_due, log_work, watch, unwatch, set_story_points, search, add_comment, dev_task, unknown. "
    'dev_task = explicit code work requests (fix, implement, refactor, write code/test, debug). '
    'For dev_task include key (ticket if mentioned) and task (the full instruction). '
    'Example: "fix the bug in STO-54171" -> {"action":"dev_task","key":"STO-54171","task":"fix the bug in STO-54171"}. '
    'Example: "implement day 1 of STO-53566" -> {"action":"dev_task","key":"STO-53566","task":"implement day 1 of STO-53566"}. '
    "Use the provided last ticket key when the user refers to a ticket implicitly. "
    "If the user mentions only a bare number like 51126, resolve it with the default project key. "
    "For transition, assign, set_priority, set_due, log_work, set_story_points, include the needed fields. "
    "For search include query. "
    "For add_comment include key, body (only the comment text, no names), and mentions (list of full names to tag, or []). "
    "add_comment = posting/writing a new comment. get_comments = reading existing comments. "
    "When user says 'tag X', 'mention X', or 'notify X', put X in mentions list, NOT in body. "
    'Example: "add comment to STO-12 that this is in UAT" -> {"action":"add_comment","key":"STO-12","body":"this is in UAT","mentions":[]}. '
    'Example: "Add comment to STO-48421 that this is on client UAT and tag sheetal jagadeesh" -> {"action":"add_comment","key":"STO-48421","body":"this is on client UAT","mentions":["sheetal jagadeesh"]}. '
    'Example: "comment on 48421 we ship Friday mention alice" -> {"action":"add_comment","key":"STO-48421","body":"we ship Friday","mentions":["alice"]}. '
    "If the intent is unclear, return {\"action\":\"unknown\"}."
)


def _get_client(api_key: str) -> AsyncGroq:
    global _client, _client_key
    if _client is None or _client_key != api_key:
        _client = AsyncGroq(api_key=api_key)
        _client_key = api_key
    return _client


async def parse_nl_command(text: str, default_project_key: str, api_key: str, last_ticket_key: str = "") -> dict:
    if not api_key:
        return {"action": "unknown"}

    user_payload = {
        "default_project_key": default_project_key,
        "last_ticket_key": last_ticket_key or None,
        "message": text,
    }

    try:
        client = _get_client(api_key)
        logger.info("parse_nl_command: calling Groq for NL normalization")
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _COMMAND_SYSTEM},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
            max_tokens=180,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {"action": "unknown"}
        logger.info("parse_nl_command: Groq normalized action=%s", parsed.get("action"))
        return parsed
    except Exception as e:
        logger.error("parse_nl_command: Groq API call failed - %s: %s", type(e).__name__, e)
        return {"action": "unknown"}


async def summarize_ticket(description: str, comments: list[str], api_key: str) -> str:
    if not api_key:
        logger.warning("summarize_ticket: GROQ_API_KEY is empty — skipping summary")
        return ""

    logger.info("summarize_ticket: building prompt (desc_len=%d, comments=%d)", len(description), len(comments))
    content_parts = [f"Description:\n{description}"]
    if comments:
        content_parts.append("Recent comments:\n" + "\n---\n".join(comments))
    user_content = "\n\n".join(content_parts)

    try:
        client = _get_client(api_key)
        logger.info("summarize_ticket: calling Groq API (model=llama-3.3-70b-versatile)")
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_content},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        result = response.choices[0].message.content.strip()
        logger.info("summarize_ticket: Groq responded OK (len=%d)", len(result))
        return result
    except Exception as e:
        logger.error("summarize_ticket: Groq API call failed — %s: %s", type(e).__name__, e)
        return ""
