import json
import logging
import os
from datetime import datetime, timezone, timedelta
from config import Config
from jira_client import JiraClient

STATE_FILE = "state.json"
logger = logging.getLogger(__name__)


def _adf_to_plain(node) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_adf_to_plain(c) for c in node.get("content", []))


def _body_text(body) -> str:
    if isinstance(body, dict):
        return _adf_to_plain(body).strip()
    return str(body or "").strip()


class JiraPoller:
    def __init__(self, config: Config, client: JiraClient, state_file: str = STATE_FILE):
        self._cfg = config
        self._client = client
        self._state_file = state_file
        self._state = self._load_state()
        logger.info("JiraPoller initialised — watching project=%s poll=%dmin",
                    config.default_project_key, config.poll_interval_minutes)

    def _load_state(self) -> dict:
        if os.path.exists(self._state_file):
            with open(self._state_file) as f:
                return json.load(f)
        return {"tickets": {}, "last_poll_at": None}

    def _elapsed_minutes(self) -> int:
        last = self._state.get("last_poll_at")
        if not last:
            return 24 * 60  # first ever run — look back 24 hours
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            return max(int(elapsed) + 2, self._cfg.poll_interval_minutes + 1)
        except Exception:
            return 24 * 60

    def _save_state(self) -> None:
        with open(self._state_file, "w") as f:
            json.dump(self._state, f, indent=2)

    def poll(self) -> list[dict]:
        changes = []

        # Fetch only tickets where I am assignee or reporter, updated recently
        tickets_map: dict[str, dict] = {}
        try:
            within_minutes = self._elapsed_minutes()
            # Old project-wide scan kept commented for quick rollback/reference.
            # relevant = self._client.get_project_recent_tickets(
            #     self._cfg.default_project_key, within_minutes
            # )
            relevant = self._client.get_my_relevant_tickets(
                self._cfg.default_project_key, within_minutes
            )
            for t in relevant:
                tickets_map[t["key"]] = t
            logger.info("poll: my relevant ticket scan returned %d tickets", len(relevant))
        except Exception as e:
            logger.error("poll: relevant scan failed — %s", e)

        now_utc = datetime.now(timezone.utc)
        new_ticket_cutoff = now_utc - timedelta(minutes=self._cfg.poll_interval_minutes + 1)

        for key, ticket in tickets_map.items():
            f = ticket["fields"]
            summary = f["summary"]
            current_status = f["status"]["name"]
            assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")

            prior = self._state["tickets"].get(key)
            is_new = prior is None

            if is_new:
                # Only notify about genuinely new tickets (created in this poll window)
                created_str = f.get("created", "")
                try:
                    created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if created_dt >= new_ticket_cutoff:
                        logger.info("poll: new ticket detected — %s", key)
                        changes.append({
                            "type": "new_ticket",
                            "key": key,
                            "summary": summary,
                            "assignee": assignee,
                            "status": current_status,
                        })
                except Exception:
                    pass
                prior = {
                    "status": current_status,
                    "last_comment_id": None,
                    "assignee": assignee,
                }
            else:
                # Status change
                if prior.get("status") != current_status:
                    logger.info("poll: status change %s — %s → %s", key, prior.get("status"), current_status)
                    changes.append({
                        "type": "status_change",
                        "key": key,
                        "summary": summary,
                        "old": prior["status"],
                        "new": current_status,
                    })

                # Assignee change
                if prior.get("assignee") != assignee:
                    logger.info("poll: assignee change %s — %s → %s", key, prior.get("assignee"), assignee)
                    changes.append({
                        "type": "new_assignment",
                        "key": key,
                        "summary": summary,
                        "assignee": assignee,
                    })

            # New comments — fetch comments for this ticket
            try:
                comments = self._client.get_comments(key)
            except Exception as e:
                logger.warning("poll: failed to fetch comments for %s — %s", key, e)
                comments = []

            if comments:
                latest_id = int(comments[-1]["id"])
                prior_last_id = prior.get("last_comment_id")

                if prior_last_id is None and is_new:
                    # First time seeing this ticket — notify only about comments
                    # created within the poll window so we don't miss live updates
                    cutoff = now_utc - timedelta(minutes=self._cfg.poll_interval_minutes + 1)
                    for c in comments:
                        created_str = c.get("created", "")
                        try:
                            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        except Exception:
                            continue
                        if created_dt >= cutoff:
                            author = c["author"]["displayName"]
                            body = _body_text(c.get("body", ""))
                            logger.info("poll: recent comment on new-to-state ticket %s by %s", key, author)
                            changes.append({
                                "type": "new_comment",
                                "key": key,
                                "summary": summary,
                                "author": author,
                                "body": body[:200],
                            })
                elif prior_last_id is None and not is_new:
                    # Ticket already in state but comment ID was never recorded
                    # (e.g. ticket had no comments when first seen). Initialise
                    # silently so future comments are detected correctly.
                    logger.debug("poll: initialising last_comment_id for %s (was null)", key)
                elif prior_last_id is not None and latest_id != int(prior_last_id):
                    new_comments = [c for c in comments if int(c["id"]) > int(prior_last_id)]
                    for c in new_comments:
                        author = c["author"]["displayName"]
                        body = _body_text(c.get("body", ""))
                        logger.info("poll: new comment on %s by %s", key, author)
                        changes.append({
                            "type": "new_comment",
                            "key": key,
                            "summary": summary,
                            "author": author,
                            "body": body[:200],
                        })

                prior["last_comment_id"] = latest_id

            prior["status"] = current_status
            prior["assignee"] = assignee
            self._state["tickets"][key] = prior

        self._state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()
        logger.info("poll: done — %d change(s) detected", len(changes))
        return changes
