import requests
from requests.auth import HTTPBasicAuth
from config import Config


class JiraClient:
    def __init__(self, config: Config):
        self._cfg = config
        self._auth = HTTPBasicAuth(config.jira_email, config.jira_api_token)
        self._api_version = "3" if config.is_jira_cloud else "2"
        self._base = f"{config.jira_base_url}/rest/api/{self._api_version}"
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(f"{self._base}{path}", auth=self._auth, headers=self._headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(f"{self._base}{path}", auth=self._auth, headers=self._headers, json=body)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def _put(self, path: str, body: dict) -> None:
        resp = requests.put(f"{self._base}{path}", auth=self._auth, headers=self._headers, json=body)
        resp.raise_for_status()

    def _delete(self, path: str, params: dict = None) -> None:
        resp = requests.delete(f"{self._base}{path}", auth=self._auth, headers=self._headers, params=params)
        resp.raise_for_status()

    # ── Read ──────────────────────────────────────────────────────────────────

    def _search(self, jql: str, fields: str, max_results: int = 50) -> list[dict]:
        data = self._get("/search/jql", {"jql": jql, "maxResults": max_results, "fields": fields})
        return data.get("issues", [])

    def get_my_tickets(self) -> list[dict]:
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        return self._search(jql, "summary,status,priority,duedate,assignee")

    def get_my_report_tickets(self, extra_fields: list[str] = None) -> list[dict]:
        fields = ["summary", "status", "priority", "duedate", "assignee", "comment", "updated"]
        if extra_fields:
            for field in extra_fields:
                if field and field not in fields:
                    fields.append(field)
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        return self._search(jql, ",".join(fields), max_results=50)

    def get_ticket(self, key: str) -> dict:
        return self._get(f"/issue/{key}")

    def get_transitions(self, key: str) -> list[dict]:
        data = self._get(f"/issue/{key}/transitions")
        return data.get("transitions", [])

    def get_comments(self, key: str) -> list[dict]:
        data = self._get(f"/issue/{key}/comment")
        return data.get("comments", [])

    def search_tickets(self, text: str) -> list[dict]:
        jql = f'text ~ "{text}" ORDER BY updated DESC'
        return self._search(jql, "summary,status,priority,duedate,assignee", max_results=20)

    def get_activity_today(self, project_key: str = "") -> list[dict]:
        if project_key:
            jql = (f"project = {project_key} AND updated >= startOfDay() "
                   f"ORDER BY updated DESC")
        else:
            jql = "assignee = currentUser() AND updated >= startOfDay() ORDER BY updated DESC"
        return self._search(jql, "summary,status,priority,assignee,comment,worklog", max_results=100)

    def get_project_recent_tickets(self, project_key: str, within_minutes: int = 6) -> list[dict]:
        jql = (
            f"project = {project_key} AND updated >= -{within_minutes}m "
            f"ORDER BY updated DESC"
        )
        return self._search(
            jql,
            "summary,status,assignee,priority,comment,created,updated",
            max_results=50,
        )

    def get_my_relevant_tickets(self, project_key: str, within_minutes: int = 6) -> list[dict]:
        """Tickets updated recently where I am assignee or reporter."""
        jql = (
            f"project = {project_key} AND updated >= -{within_minutes}m "
            f"AND (assignee = currentUser() OR reporter = currentUser()) "
            f"ORDER BY updated DESC"
        )
        return self._search(
            jql,
            "summary,status,assignee,priority,comment,created,updated",
            max_results=50,
        )

    def get_due_soon(self, within_hours: int = 48) -> list[dict]:
        jql = f"assignee = currentUser() AND due <= {within_hours}h AND statusCategory != Done ORDER BY due ASC"
        return self._search(jql, "summary,status,duedate", max_results=20)

    def search_user(self, query: str) -> list[dict]:
        return self._get("/user/search", {"query": query, "maxResults": 5})

    def get_current_user_account_id(self) -> str:
        if not hasattr(self, "_my_account_id"):
            data = self._get("/myself")
            self._my_account_id = data["accountId"]
        return self._my_account_id

    # ── Write ─────────────────────────────────────────────────────────────────

    def do_transition(self, key: str, status_name: str) -> None:
        transitions = self.get_transitions(key)
        match = next(
            (t for t in transitions if t["name"].lower() == status_name.lower()),
            None
        )
        # Fall back: match by destination status name (e.g. "Architecture Review" → "Review Successful")
        if not match:
            match = next(
                (t for t in transitions
                 if t.get("to", {}).get("name", "").lower() == status_name.lower()),
                None
            )
        if not match:
            available = ", ".join(t["name"] for t in transitions)
            raise ValueError(f"Invalid status '{status_name}'. Available: {available}")
        self._post(f"/issue/{key}/transitions", {"transition": {"id": match["id"]}})

    def set_story_points(self, key: str, points: float) -> None:
        self._put(f"/issue/{key}", {"fields": {"customfield_10016": points}})

    def add_comment(self, key: str, text: str, mentions: list[str] = None) -> dict:
        if self._cfg.is_jira_cloud:
            body = self._build_adf_comment(text)
        else:
            body = self._build_wiki_comment(text)
        return self._post(f"/issue/{key}/comment", {"body": body})

    def _build_adf_comment(self, text: str) -> dict:
        import re
        parts = re.split(r"(@\w+(?:\.\w+)*)", text)
        inline = []
        for part in parts:
            if part.startswith("@"):
                name = part[1:]
                users = self.search_user(name)
                if users:
                    inline.append({
                        "type": "mention",
                        "attrs": {"id": users[0]["accountId"], "text": part}
                    })
                else:
                    inline.append({"type": "text", "text": part})
            elif part:
                inline.append({"type": "text", "text": part})
        return {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": inline}]
        }

    def _build_wiki_comment(self, text: str) -> str:
        import re
        def replace_mention(m):
            name = m.group(1)
            users = self.search_user(name)
            if users:
                username = users[0].get("name", name)
                return f"[~{username}]"
            return m.group(0)
        return re.sub(r"@(\w+(?:\.\w+)*)", replace_mention, text)

    def assign_ticket(self, key: str, name: str) -> None:
        users = self.search_user(name)
        if not users:
            raise ValueError(f"User '{name}' not found in Jira")
        if self._cfg.is_jira_cloud:
            self._put(f"/issue/{key}", {"fields": {"assignee": {"accountId": users[0]["accountId"]}}})
        else:
            self._put(f"/issue/{key}", {"fields": {"assignee": {"name": users[0].get("name")}}})

    def set_priority(self, key: str, priority: str) -> None:
        self._put(f"/issue/{key}", {"fields": {"priority": {"name": priority}}})

    def set_due_date(self, key: str, date: str) -> None:
        self._put(f"/issue/{key}", {"fields": {"duedate": date}})

    def add_label(self, key: str, label: str) -> None:
        ticket = self.get_ticket(key)
        labels = ticket["fields"].get("labels", [])
        if label not in labels:
            labels.append(label)
        self._put(f"/issue/{key}", {"fields": {"labels": labels}})

    def remove_label(self, key: str, label: str) -> None:
        ticket = self.get_ticket(key)
        labels = [l for l in ticket["fields"].get("labels", []) if l != label]
        self._put(f"/issue/{key}", {"fields": {"labels": labels}})

    def link_tickets(self, key1: str, link_type: str, key2: str) -> None:
        self._post("/issueLink", {
            "type": {"name": link_type},
            "inwardIssue": {"key": key1},
            "outwardIssue": {"key": key2},
        })

    def log_work(self, key: str, time_spent: str) -> dict:
        return self._post(f"/issue/{key}/worklog", {"timeSpent": time_spent})

    def watch_ticket(self, key: str) -> None:
        account_id = self.get_current_user_account_id()
        resp = requests.post(
            f"{self._base}/issue/{key}/watchers",
            auth=self._auth,
            headers=self._headers,
            json=account_id,
        )
        resp.raise_for_status()

    def unwatch_ticket(self, key: str) -> None:
        account_id = self.get_current_user_account_id()
        self._delete(f"/issue/{key}/watchers", params={"accountId": account_id})

    def create_ticket(self, project: str, title: str, description: str = "", assignee_name: str = None) -> dict:
        fields: dict = {
            "project": {"key": project},
            "summary": title,
            "issuetype": {"name": "Task"},
        }
        if description:
            if self._cfg.is_jira_cloud:
                fields["description"] = {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
                }
            else:
                fields["description"] = description
        if assignee_name:
            users = self.search_user(assignee_name)
            if users:
                if self._cfg.is_jira_cloud:
                    fields["assignee"] = {"accountId": users[0]["accountId"]}
                else:
                    fields["assignee"] = {"name": users[0].get("name")}
        return self._post("/issue", {"fields": fields})
