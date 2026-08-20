import logging
from jira_client import JiraClient

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a Jira EOD report assistant. "
    "Given a list of Jira tickets updated today, write a concise end-of-day summary. "
    "Group tickets by status: Done/Completed, In Progress, Pending/Blocked. "
    "Keep it under 200 words. Plain text only, no markdown, no bullet symbols."
)


def generate_eod_report(jira: JiraClient, groq_api_key: str = "", project_key: str = "") -> str:
    logger.info("EOD report: fetching today's activity (project=%s)", project_key or "my tickets")
    try:
        tickets = jira.get_activity_today(project_key=project_key)
    except Exception as e:
        logger.error("EOD report: failed to fetch tickets — %s", e)
        return "Failed to fetch today's Jira activity."

    if not tickets:
        return "No Jira activity found for today."

    logger.info("EOD report: %d tickets updated today", len(tickets))

    data = []
    for t in tickets:
        f = t["fields"]
        assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")
        data.append({
            "key": t["key"],
            "summary": f["summary"],
            "status": f["status"]["name"],
            "assignee": assignee,
        })

    if groq_api_key:
        summary = _summarize_with_groq(data, groq_api_key)
        if summary:
            return summary

    # Plain fallback
    lines = ["*EOD Activity Summary*\n"]
    for t in data:
        lines.append(f"• *{t['key']}*: {t['summary']} ({t['status']}) — {t['assignee']}")
    return "\n".join(lines)


def _summarize_with_groq(data: list[dict], api_key: str) -> str:
    try:
        from groq import Groq
        import json
        client = Groq(api_key=api_key)
        logger.info("EOD report: calling Groq for summary (%d tickets)", len(data))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "Today's tickets:\n" + json.dumps(data, indent=2)},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        result = response.choices[0].message.content.strip()
        logger.info("EOD report: Groq responded OK (len=%d)", len(result))
        return result
    except Exception as e:
        logger.error("EOD report: Groq failed — %s", e)
        return ""
