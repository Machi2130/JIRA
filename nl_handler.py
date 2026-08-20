import re

# Matches full Jira keys (e.g. STO-48421) or bare numbers (e.g. 48421)
_KEY_PATTERN = re.compile(r'\b([A-Z]+-\d+|\d{3,6})\b', re.IGNORECASE)

_DEV_TASK_WORDS = re.compile(
    r'\b(fix|implement|refactor|write\s+(code|test|unit\s+test)|add\s+feature|develop|build\s+code|'
    r'create\s+(test|feature)|debug|investigate\s+code|claude)\b',
    re.IGNORECASE,
)

_TRANSITION_WORDS = re.compile(
    r'\b(make|move|change|set status|transition|mark|close|reopen|start|complete|finish|done|resolve)\b',
    re.IGNORECASE,
)
# Matches "to X" or "as X" stopping before "and", a digit, or end of clause
_STATUS_TO_PATTERN = re.compile(
    r'\b(?:to|as)\s+([a-z][a-z\s]*?)(?=\s+and\b|\s+of\b|\s+[a-z]+-\d+\b|\s*\d|\s*$|\s*[,!?.])',
    re.IGNORECASE,
)
# Fallback: "status X" without a "to" preposition (e.g. "set status In Progress")
_STATUS_BARE_PATTERN = re.compile(
    r'\bstatus\s+([a-z][a-z\s]*?)(?=\s+and\b|\s+of\b|\s+[a-z]+-\d+\b|\s*\d|\s*$|\s*[,!?.])',
    re.IGNORECASE,
)
_SP_VALUE = re.compile(
    r'\b(?:story[\s-]*points?|sp)\s+(?:to\s+)?(\d+(?:\.\d+)?)'
    r'|\b(\d+(?:\.\d+)?)\s+story[\s-]*points?\b',
    re.IGNORECASE,
)
_COMMENT_WORDS = re.compile(r'\bcomments?\b', re.IGNORECASE)
_ADD_COMMENT_WORDS = re.compile(r'\b(add|post|write|leave|put|saying|say)\b', re.IGNORECASE)
_TRANSITIONS_WORDS = re.compile(r'\b(transitions?|available\s+statuses?|available\s+status)\b', re.IGNORECASE)
_ASSIGN_WORDS = re.compile(r'\b(assign|reassign)\b', re.IGNORECASE)
_PRIORITY_WORDS = re.compile(r'\b(priority|priorit(ize|y))\b', re.IGNORECASE)
_DUE_SET_WORDS = re.compile(r'\b(set\s+due|due\s+date|setdue)\b', re.IGNORECASE)
_LOG_WORDS = re.compile(r'\b(log|track)\s+(time|work|hours?)\b', re.IGNORECASE)
_TIME_SPENT = re.compile(r'\b(\d+(?:\.\d+)?[hHmM])\b')
_WATCH_WORDS = re.compile(r'\b(watch|unwatch|stop\s+watching)\b', re.IGNORECASE)
_LINK_WORDS = re.compile(r'\b(link|blocks?|relates?\s+to|duplicates?)\b', re.IGNORECASE)
_SEARCH_WORDS = re.compile(r'^(search|find|look\s+up|lookup)\s+(.+)', re.IGNORECASE)
_MY_TICKETS = re.compile(r'\b(my\s+tickets?|open\s+tickets?|show\s+tickets?|list\s+tickets?)\b', re.IGNORECASE)
_DUE_SOON = re.compile(r'\b(due\s+soon|coming\s+due|deadline|overdue)\b', re.IGNORECASE)
_REPORT_WORDS = re.compile(r'\b(report|eod|end\s+of\s+day|summary)\b', re.IGNORECASE)
_WORKFLOW_WORDS = re.compile(
    r'\b(workflow|flow|next\s+status|what.*next|what.*status|when.*move|how.*move)\b',
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')
_PRIORITY_VALUE = re.compile(r'\b(highest|high|medium|low|lowest|critical|major|minor|blocker)\b', re.IGNORECASE)


def _resolve_key(raw: str, default_project: str) -> str:
    if re.match(r'^\d+$', raw):
        return f"{default_project}-{raw}"
    return raw.upper()


def parse(text: str, default_project: str = "STO") -> dict:
    """
    Parse a plain-text message into a structured command dict.
    Returns {"action": <str>, ...fields} or {"action": "unknown"}.
    Story points are detected as a secondary field alongside any primary action.
    """
    t = text.strip()
    result = _parse_primary(t, default_project)

    # Augment with story points if present in the same message
    key = result.get("key")
    if key and result.get("action") != "set_story_points":
        sp_m = _SP_VALUE.search(t)
        if sp_m:
            result["story_points"] = float(sp_m.group(1) or sp_m.group(2))

    return result


def _parse_primary(t: str, default_project: str) -> dict:
    # Dev task — only when explicitly triggered
    if _DEV_TASK_WORDS.search(t):
        keys = _KEY_PATTERN.findall(t)
        key = _resolve_key(keys[0], default_project) if keys else None
        return {"action": "dev_task", "task": t, "key": key}

    # My tickets list
    if _MY_TICKETS.search(t):
        return {"action": "get_tickets"}

    # Due soon
    if _DUE_SOON.search(t):
        return {"action": "get_due_soon"}

    # EOD report
    if _REPORT_WORDS.search(t):
        return {"action": "get_report"}

    # Workflow / what to do next
    if _WORKFLOW_WORDS.search(t):
        keys = _KEY_PATTERN.findall(t)
        key = _resolve_key(keys[0], default_project) if keys else None
        return {"action": "get_workflow", "key": key}

    # Search (must come before key detection so "search login bug" isn't parsed as key)
    m = _SEARCH_WORDS.match(t)
    if m:
        return {"action": "search", "query": m.group(2).strip()}

    # Find ticket key in message
    keys = _KEY_PATTERN.findall(t)
    key = _resolve_key(keys[0], default_project) if keys else None

    if not key:
        return {"action": "unknown"}

    # Status transition first so "change the status to qa of jira 51126"
    # is treated as an action, not as a request for available statuses.
    if _TRANSITION_WORDS.search(t):
        m = _STATUS_TO_PATTERN.search(t)
        if not m:
            m = _STATUS_BARE_PATTERN.search(t)
        if m:
            status = m.group(1).strip().rstrip(".,!?")
            return {"action": "transition", "key": key, "status": status}
        # Bare shorthand: "done 48421", "close 48421"
        shorthand = re.match(r'^(done|close|resolve|start|reopen)\b', t, re.IGNORECASE)
        if shorthand:
            status_map = {
                "done": "Done", "close": "Done", "resolve": "Done",
                "start": "In Progress", "reopen": "To Do",
            }
            return {"action": "transition", "key": key, "status": status_map[shorthand.group(1).lower()]}

    # Comments — write vs read
    if _COMMENT_WORDS.search(t):
        if _ADD_COMMENT_WORDS.search(t):
            # Only extract body when there's a clear delimiter (colon or em-dash).
            # Conversational phrasing ("add comment to this jira that...") has no delimiter
            # and should fall through to the Groq fallback for reliable body extraction.
            body_m = re.search(r'comment\s*[:\-—]\s*(.+)', t, re.IGNORECASE | re.DOTALL)
            body = body_m.group(1).strip() if body_m else ""
            return {"action": "add_comment", "key": key, "body": body}
        return {"action": "get_comments", "key": key}

    # Transitions/statuses list
    if _TRANSITIONS_WORDS.search(t):
        return {"action": "get_transitions", "key": key}

    # Status transition — "move X to Done" / "mark X as In Progress" / "change status to X"
    if _TRANSITION_WORDS.search(t):
        m = _STATUS_TO_PATTERN.search(t)
        if not m:
            m = _STATUS_BARE_PATTERN.search(t)
        if m:
            status = m.group(1).strip().rstrip(".,!?")
            return {"action": "transition", "key": key, "status": status}
        # Bare shorthand: "done 48421", "close 48421"
        shorthand = re.match(r'^(done|close|resolve|start|reopen)\b', t, re.IGNORECASE)
        if shorthand:
            status_map = {
                "done": "Done", "close": "Done", "resolve": "Done",
                "start": "In Progress", "reopen": "To Do",
            }
            return {"action": "transition", "key": key, "status": status_map[shorthand.group(1).lower()]}

    # Assign
    if _ASSIGN_WORDS.search(t):
        m = re.search(r'(?:assign\w*)\s+\S+\s+(?:to\s+)?(.+)', t, re.IGNORECASE)
        name = m.group(1).strip() if m else "me"
        return {"action": "assign", "key": key, "name": name}

    # Priority
    if _PRIORITY_WORDS.search(t):
        m = _PRIORITY_VALUE.search(t)
        priority = m.group(1).capitalize() if m else None
        if priority:
            return {"action": "set_priority", "key": key, "priority": priority}

    # Set due date
    if _DUE_SET_WORDS.search(t):
        m = _DATE_PATTERN.search(t)
        if m:
            return {"action": "set_due", "key": key, "date": m.group(1)}

    # Log work
    if _LOG_WORDS.search(t):
        m = _TIME_SPENT.search(t)
        if m:
            return {"action": "log_work", "key": key, "time_spent": m.group(1)}

    # Watch / unwatch
    if _WATCH_WORDS.search(t):
        action = "unwatch" if re.search(r'\b(unwatch|stop)', t, re.IGNORECASE) else "watch"
        return {"action": action, "key": key}

    # Story points only (no other action matched)
    sp_m = _SP_VALUE.search(t)
    if sp_m:
        return {"action": "set_story_points", "key": key,
                "story_points": float(sp_m.group(1) or sp_m.group(2))}

    # Default: show ticket details when only a key (or bare number) is mentioned
    return {"action": "get_ticket", "key": key}
 