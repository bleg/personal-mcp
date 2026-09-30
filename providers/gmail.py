"""Gmail via the Gmail REST API (auth in google_auth.py). Uses gmail.readonly + gmail.compose.

gmail.compose is the narrowest scope that allows drafts, but Google's scope also
permits sending. This module must never call messages.send / drafts.send.
"""
import base64
import datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import html2text

from providers import google_auth

API = "https://gmail.googleapis.com/gmail/v1/users/me"
BODY_LIMIT = 20000

_h2t = html2text.HTML2Text()
_h2t.ignore_images = True
_h2t.body_width = 0


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _iso(date_header: str) -> str:
    try:
        return parsedate_to_datetime(date_header).astimezone(datetime.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return date_header


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to html converted to text."""
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and not part.get("filename"):
            if mime == "text/plain":
                plain.append(_decode(data))
            elif mime == "text/html":
                html.append(_decode(data))
        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    return _h2t.handle("\n".join(html)) if html else ""


def _attachments(payload: dict) -> list[dict]:
    out = []

    def walk(part: dict) -> None:
        if part.get("filename"):
            out.append({
                "name": part["filename"],
                "type": part.get("mimeType"),
                "size": part.get("body", {}).get("size"),
            })
        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    return out


class GmailProvider:
    name = "gmail"

    def login(self, account: str) -> str:
        return google_auth.login(account)

    def _req(self, account: str, method: str, path: str, **kw) -> dict:
        scope = google_auth.GMAIL_COMPOSE if method == "POST" else google_auth.GMAIL_READ
        return google_auth.request(account, scope, method, f"{API}{path}", **kw).json()

    @staticmethod
    def _summary(m: dict) -> dict:
        return {
            "id": m["id"],
            "conversation_id": m.get("threadId"),
            "subject": _header(m, "Subject"),
            "from": _header(m, "From"),
            "date": _iso(_header(m, "Date")),
            "preview": m.get("snippet"),
        }

    _META = {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}

    def search(self, account: str, query: str, limit: int = 10) -> list[dict]:
        ids = self._req(
            account, "GET", "/messages", params={"q": query, "maxResults": min(limit, 50)}
        ).get("messages", [])
        return [
            self._summary(self._req(account, "GET", f"/messages/{i['id']}", params=self._META))
            for i in ids
        ]

    def get_message(self, account: str, message_id: str) -> dict:
        m = self._req(account, "GET", f"/messages/{message_id}", params={"format": "full"})
        text = _body_text(m["payload"])
        out = self._summary(m)
        out.pop("preview")
        out["to"] = _header(m, "To")
        out["cc"] = _header(m, "Cc")
        out["body"] = text[:BODY_LIMIT]
        out["truncated"] = len(text) > BODY_LIMIT
        out["attachments"] = _attachments(m["payload"])
        return out

    def get_conversation(self, account: str, conversation_id: str) -> list[dict]:
        t = self._req(account, "GET", f"/threads/{conversation_id}", params=self._META)
        msgs = sorted(t.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
        return [self._summary(m) for m in msgs]

    def create_draft(
        self,
        account: str,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Save a draft. Never sends."""
        msg = EmailMessage()
        thread_id = None
        if reply_to_message_id:
            orig = self._req(
                account, "GET", f"/messages/{reply_to_message_id}",
                params={"format": "metadata",
                        "metadataHeaders": ["From", "Reply-To", "Subject", "Message-ID", "References"]},
            )
            subj = _header(orig, "Subject")
            msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
            msg["To"] = ", ".join(to) or (_header(orig, "Reply-To") or _header(orig, "From"))
            mid = _header(orig, "Message-ID")
            if mid:
                msg["In-Reply-To"] = mid
                msg["References"] = f'{_header(orig, "References")} {mid}'.strip()
            thread_id = orig["threadId"]
        else:
            msg["Subject"] = subject
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.set_content(body)
        message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            message["threadId"] = thread_id
        d = self._req(account, "POST", "/drafts", json={"message": message})
        return {"id": d["id"], "subject": msg["Subject"], "status": "saved as draft, NOT sent"}
