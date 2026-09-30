"""Google Drive. Reads with drive.readonly; writes with drive.file, which Google
limits to files this app created. Files are also tagged in appProperties and every
edit or trash re-checks the tag. Nothing is ever permanently deleted."""
import json
from providers import google_auth

API = "https://www.googleapis.com/drive/v3"
BODY_LIMIT = 20000
FIELDS = "id,name,mimeType,modifiedTime,owners(emailAddress),webViewLink,size,appProperties"

# Google-native files can't be downloaded directly; export them as text.
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _get(account: str, path: str, **params) -> "httpx.Response":
    return google_auth.request(
        account, google_auth.DRIVE_READ, "GET", f"{API}{path}",
        params={"supportsAllDrives": "true", **params},
    )


def search(account: str, query: str, limit: int = 10) -> list[dict]:
    q = query.replace("\\", "\\\\").replace("'", "\\'")
    r = _get(
        account, "/files",
        q=f"fullText contains '{q}' and trashed = false",
        pageSize=min(limit, 50),
        fields=f"files({FIELDS})",
        includeItemsFromAllDrives="true",
        orderBy="modifiedTime desc",
    ).json()
    return [_summary(f) for f in r.get("files", [])]


def _summary(f: dict) -> dict:
    return {
        "id": f["id"],
        "name": f["name"],
        "type": f["mimeType"],
        "modified": f.get("modifiedTime"),
        "owner": (f.get("owners") or [{}])[0].get("emailAddress"),
        "link": f.get("webViewLink"),
    }


def get_file(account: str, file_id: str) -> dict:
    meta = _get(account, f"/files/{file_id}", fields=FIELDS).json()
    out = _summary(meta)
    mime = meta["mimeType"]
    if mime in EXPORTS:
        text = _get(account, f"/files/{file_id}/export", mimeType=EXPORTS[mime]).text
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        text = _get(account, f"/files/{file_id}", alt="media").text
    else:
        out["content"] = None
        out["note"] = f"Content not retrieved: {mime} is not a text or Google-native file."
        return out
    text = text.lstrip("\ufeff")  # Google exports start with a byte-order mark
    out["content"] = text[:BODY_LIMIT]
    out["truncated"] = len(text) > BODY_LIMIT
    return out


UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
KINDS = {  # kind -> (Drive mimeType to store as, media type of the content we send)
    "doc": ("application/vnd.google-apps.document", "text/plain"),
    "sheet": ("application/vnd.google-apps.spreadsheet", "text/csv"),
    "text": ("text/plain", "text/plain"),
}


def _multipart(meta: dict, content: str, media_type: str) -> tuple[bytes, dict]:
    b = "personal-mcp-boundary-7f3a9"
    body = (
        f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
        f"--{b}\r\nContent-Type: {media_type}; charset=UTF-8\r\n\r\n{content}\r\n--{b}--"
    ).encode()
    return body, {"Content-Type": f"multipart/related; boundary={b}"}


def _require_ours(account: str, file_id: str) -> dict:
    meta = _get(account, f"/files/{file_id}", fields=FIELDS).json()
    if (meta.get("appProperties") or {}).get("createdBy") != google_auth.CREATED_BY["createdBy"]:
        raise PermissionError("Refusing: this file was not created by personal-mcp.")
    return meta


def create_file(account: str, name: str, content: str, kind: str = "doc") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    stored, media = KINDS[kind]
    meta = {"name": name, "mimeType": stored, "appProperties": google_auth.CREATED_BY}
    body, headers = _multipart(meta, content, media)
    r = google_auth.request(
        account, google_auth.DRIVE_FILE, "POST", UPLOAD,
        params={"uploadType": "multipart", "fields": FIELDS}, content=body, headers=headers,
    ).json()
    return _summary(r)


def update_file(account: str, file_id: str, content: str) -> dict:
    """Replace the whole content of a file this MCP created."""
    meta = _require_ours(account, file_id)
    media = next((m for s, m in KINDS.values() if s == meta["mimeType"]), "text/plain")
    body, headers = _multipart({}, content, media)
    r = google_auth.request(
        account, google_auth.DRIVE_FILE, "PATCH", f"{UPLOAD}/{file_id}",
        params={"uploadType": "multipart", "fields": FIELDS}, content=body, headers=headers,
    ).json()
    return _summary(r)


def trash_file(account: str, file_id: str) -> dict:
    """Move to Trash (recoverable). Only files this MCP created."""
    _require_ours(account, file_id)
    google_auth.request(
        account, google_auth.DRIVE_FILE, "PATCH", f"{API}/files/{file_id}", json={"trashed": True}
    )
    return {"id": file_id, "status": "moved to Trash (recoverable)"}
