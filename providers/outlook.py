"""Hotmail / Outlook.com via Microsoft Graph. Mail.ReadWrite is needed only because
Graph has no draft-only scope; this module must never send, edit or delete mail."""
import os

import html2text
import httpx
import msal
import tokenstore

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.ReadWrite", "User.Read"]  # msal adds offline_access itself
GRAPH = "https://graph.microsoft.com/v1.0"
KEYRING_SERVICE = "personal-mcp"
BODY_LIMIT = 20000

_h2t = html2text.HTML2Text()
_h2t.ignore_images = True
_h2t.body_width = 0


class OutlookProvider:
    name = "outlook"

    def __init__(self) -> None:
        self.client_id = os.environ.get("MS_CLIENT_ID")

    def _app(self, account: str):
        if not self.client_id:
            raise RuntimeError("MS_CLIENT_ID is not set")
        cache = msal.SerializableTokenCache()
        blob = tokenstore.get(KEYRING_SERVICE, account)
        if blob:
            cache.deserialize(blob)
        app = msal.PublicClientApplication(
            self.client_id, authority=AUTHORITY, token_cache=cache
        )
        return app, cache

    def _save(self, account: str, cache) -> None:
        if cache.has_state_changed:
            tokenstore.set(KEYRING_SERVICE, account, cache.serialize())

    def login(self, account: str) -> str:
        """Interactive browser login. Run once via login.py, not from the MCP."""
        app, cache = self._app(account)
        result = app.acquire_token_interactive(SCOPES)
        if "access_token" not in result:
            raise RuntimeError(result.get("error_description", "login failed"))
        self._save(account, cache)
        return result["id_token_claims"].get("preferred_username", account)

    def _token(self, account: str) -> str:
        app, cache = self._app(account)
        accounts = app.get_accounts()
        if not accounts:
            raise RuntimeError(f"Account '{account}' not logged in; run login.py")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError(f"Token refresh failed for '{account}'; run login.py")
        self._save(account, cache)
        return result["access_token"]

    def _get(self, account: str, path: str, params: dict | None = None) -> dict:
        r = httpx.get(
            f"{GRAPH}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._token(account)}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _summary(m: dict) -> dict:
        frm = (m.get("from") or {}).get("emailAddress", {})
        return {
            "id": m["id"],
            "conversation_id": m.get("conversationId"),
            "subject": m.get("subject"),
            "from": f'{frm.get("name", "")} <{frm.get("address", "")}>',
            "date": m.get("receivedDateTime"),
            "preview": m.get("bodyPreview"),
        }

    def search(self, account: str, query: str, limit: int = 10) -> list[dict]:
        q = query.replace('"', " ")
        data = self._get(
            account,
            "/me/messages",
            {
                "$search": f'"{q}"',
                "$top": min(limit, 50),
                "$select": "id,conversationId,subject,from,receivedDateTime,bodyPreview",
            },
        )
        return [self._summary(m) for m in data.get("value", [])]

    def get_message(self, account: str, message_id: str) -> dict:
        m = self._get(
            account,
            f"/me/messages/{message_id}",
            {"$expand": "attachments($select=name,contentType,size)"},
        )
        body = m.get("body", {})
        text = body.get("content", "")
        if body.get("contentType") == "html":
            text = _h2t.handle(text)
        out = self._summary(m)
        out.pop("preview")
        out["to"] = [r["emailAddress"]["address"] for r in m.get("toRecipients", [])]
        out["cc"] = [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])]
        out["body"] = text[:BODY_LIMIT]
        out["truncated"] = len(text) > BODY_LIMIT
        out["attachments"] = [
            {"name": a["name"], "type": a["contentType"], "size": a["size"]}
            for a in m.get("attachments", [])
        ]
        return out

    def get_conversation(self, account: str, conversation_id: str) -> list[dict]:
        safe = conversation_id.replace("'", "''")
        data = self._get(
            account,
            "/me/messages",
            {
                "$filter": f"conversationId eq '{safe}'",
                "$top": 50,
                "$select": "id,conversationId,subject,from,receivedDateTime,bodyPreview",
            },
        )
        msgs = [self._summary(m) for m in data.get("value", [])]
        return sorted(msgs, key=lambda m: m["date"] or "")

    def create_draft(
        self,
        account: str,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Save a draft in the Drafts folder. Never sends."""
        token = self._token(account)
        headers = {"Authorization": f"Bearer {token}"}

        def recipients(addrs):
            return [{"emailAddress": {"address": a}} for a in addrs]

        if reply_to_message_id:
            # createReply inserts `comment` above the quoted original.
            r = httpx.post(
                f"{GRAPH}/me/messages/{reply_to_message_id}/createReply",
                json={"comment": body},
                headers=headers,
                timeout=30,
            )
        else:
            r = httpx.post(
                f"{GRAPH}/me/messages",
                json={
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": recipients(to),
                    "ccRecipients": recipients(cc or []),
                },
                headers=headers,
                timeout=30,
            )
        r.raise_for_status()
        d = r.json()
        return {"id": d["id"], "subject": d.get("subject"), "web_link": d.get("webLink"), "status": "saved as draft, NOT sent"}
