"""
STO Jira workflow knowledge — statuses, transitions, and what comes next.
"""

# Each status maps to the transitions you can take from it.
WORKFLOW: dict[str, list[dict]] = {
    "TO BE DONE": [
        {"transition": "Start Development",       "to": "DEV IN PROGRESS"},
        {"transition": "Ready For Handover",       "to": "READY FOR ENGG SPRINT"},
        {"transition": "Awaiting Client Response", "to": "AWAITING CLIENT RESPONSE"},
        {"transition": "Not an issue",             "to": "DROPPED"},
        {"transition": "Move to Backlog",          "to": "BACKLOG"},
    ],
    "DEV IN PROGRESS": [
        {"transition": "Finish Development",       "to": "CODE REVIEW"},
        {"transition": "Back to To Do",            "to": "TO BE DONE"},
        {"transition": "Awaiting Client Response", "to": "AWAITING CLIENT RESPONSE"},
        {"transition": "Not an issue",             "to": "DROPPED"},
    ],
    "CODE REVIEW": [
        {"transition": "Review Successful",        "to": "ARCHITECTURE REVIEW"},
        {"transition": "Review Failed",            "to": "TO BE DONE"},
        {"transition": "Test failed",              "to": "CODE REVIEW"},
    ],
    "ARCHITECTURE REVIEW": [
        {"transition": "Architecture Review Successful", "to": "QA"},
        {"transition": "Review Failed",                  "to": "TO BE DONE"},
    ],
    "QA": [
        {"transition": "In Progress",              "to": "QA - IN PROGRESS"},
    ],
    "QA - IN PROGRESS": [
        {"transition": "UAT by PM",                "to": "UAT"},
        {"transition": "In Progress",              "to": "QA - IN PROGRESS"},
        {"transition": "Verification Failed",      "to": "QA - IN PROGRESS"},
    ],
    "UAT": [
        {"transition": "UAT Passed",               "to": "DEPLOYMENT - PENDING"},
        {"transition": "UAT Failed",               "to": "QA - IN PROGRESS"},
        {"transition": "UAT Pending",              "to": "UAT"},
    ],
    "DEPLOYMENT - PENDING": [
        {"transition": "Verify in Production",     "to": "PRODUCTION VERIFICATION"},
        {"transition": "UAT Failed",               "to": "QA - IN PROGRESS"},
    ],
    "PRODUCTION VERIFICATION": [
        {"transition": "Verification Success",     "to": "DONE"},
        {"transition": "Verification Failed",      "to": "QA - IN PROGRESS"},
    ],
    "AWAITING CLIENT RESPONSE": [
        {"transition": "Client Responded",         "to": "DEV IN PROGRESS"},
        {"transition": "Client Verified",          "to": "DONE"},
        {"transition": "Client Declined",          "to": "TO BE DONE"},
    ],
    "DROPPED": [
        {"transition": "Move DROPPED to To Be Done", "to": "TO BE DONE"},
    ],
    "DONE": [
        {"transition": "Move to To Be Done",       "to": "TO BE DONE"},
    ],
    "READY FOR ENGG SPRINT": [
        {"transition": "To be Done",               "to": "TO BE DONE"},
        {"transition": "Ready For Handover",       "to": "READY FOR ENGG SPRINT"},
    ],
}

# The main delivery path in order
HAPPY_PATH = [
    "TO BE DONE",
    "DEV IN PROGRESS",
    "CODE REVIEW",
    "ARCHITECTURE REVIEW",
    "QA",
    "QA - IN PROGRESS",
    "UAT",
    "DEPLOYMENT - PENDING",
    "PRODUCTION VERIFICATION",
    "DONE",
]

# What to do at each stage — plain-English guidance
STAGE_HINTS = {
    "TO BE DONE":             "Dev hasn't started. Use *Start Development* to begin.",
    "DEV IN PROGRESS":        "Dev is coding. Use *Finish Development* when code is ready for review.",
    "CODE REVIEW":            "Peer review stage. Use *Review Successful* to pass, *Review Failed* to push back.",
    "ARCHITECTURE REVIEW":    "Arch/senior review. Use *Architecture Review Successful* to send to QA.",
    "QA":                     "Assign to QA engineer here. Use *In Progress* when QA starts testing.",
    "QA - IN PROGRESS":       "QA is actively testing. Use *UAT by PM* when QA passes.",
    "UAT":                    "PM acceptance testing. Use *UAT Passed* to deploy, *UAT Failed* to send back.",
    "DEPLOYMENT - PENDING":   "Waiting for deploy. Use *Verify in Production* after deploy.",
    "PRODUCTION VERIFICATION":"Verifying in prod. Use *Verification Success* to close, *Verification Failed* to retest.",
    "DONE":                   "Ticket is closed.",
    "DROPPED":                "Ticket was cancelled.",
    "AWAITING CLIENT RESPONSE": "Waiting on client. Use *Client Responded* when they reply.",
}


def get_next_steps(current_status: str) -> list[dict]:
    return WORKFLOW.get(current_status.upper(), [])


def format_workflow(current_status: str = None) -> str:
    """Full workflow diagram with current status highlighted."""
    lines = ["*STO Workflow*\n"]
    for status in HAPPY_PATH:
        is_current = current_status and status.upper() == current_status.upper()
        marker = "▶ " if is_current else "   "
        lines.append(f"{marker}`{status}`")
        nexts = WORKFLOW.get(status, [])
        happy_next = next(
            (n for n in nexts if n["to"] in HAPPY_PATH
             and HAPPY_PATH.index(n["to"]) == HAPPY_PATH.index(status) + 1),
            None,
        )
        if happy_next and status != "DONE":
            lines.append(f"       ↓ _{happy_next['transition']}_")
    return "\n".join(lines)


def format_next_steps(current_status: str) -> str:
    """What to do right now from this status."""
    hint = STAGE_HINTS.get(current_status.upper(), "")
    nexts = get_next_steps(current_status)
    if not nexts:
        return f"No known transitions from *{current_status}*."
    lines = [f"*{current_status}*"]
    if hint:
        lines.append(f"_{hint}_\n")
    lines.append("Available transitions:")
    for n in nexts:
        lines.append(f"  • `{n['transition']}` → {n['to']}")
    return "\n".join(lines)


def find_path_to(target_status: str, from_status: str) -> str:
    """Tell the user how to reach a target status from where they are."""
    target = target_status.upper()
    current = from_status.upper()
    if current == target:
        return f"Already at *{from_status}*."
    try:
        ci = HAPPY_PATH.index(current)
        ti = HAPPY_PATH.index(target)
    except ValueError:
        return ""
    if ti <= ci:
        return f"*{target_status}* is earlier in the flow than *{from_status}* — can't move forward to it."
    steps = []
    for i in range(ci, ti):
        status = HAPPY_PATH[i]
        nexts = WORKFLOW.get(status, [])
        happy_next = next(
            (n for n in nexts if n["to"] == HAPPY_PATH[i + 1]), None
        )
        if happy_next:
            steps.append(f"  {i - ci + 1}. `{status}` → _{happy_next['transition']}_ → `{HAPPY_PATH[i+1]}`")
    if steps:
        return f"To reach *{target_status}* from *{from_status}*:\n" + "\n".join(steps)
    return ""
