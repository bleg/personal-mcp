"""Shared Google OAuth for Gmail, Drive and Calendar. One login per account.

Credentials live in the Keychain (service personal-mcp-gmail, username = account).
Writes: gmail.compose (drafts), drive.file (files this app created), calendar.events.
gmail.compose also permits sending, so no code may call messages.send or drafts.send.
calendar.events can touch any event, so calendar writes are restricted in code to events
tagged CREATED_BY; never send invitations (sendUpdates stays "none").
"""
import json
import os
import pathlib

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

import tokenstore

GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
DRIVE_READ = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"  # only files this app created
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"  # read/write events
SCOPES = [GMAIL_READ, GMAIL_COMPOSE, DRIVE_READ, DRIVE_FILE, CALENDAR_EVENTS]

CREATED_BY = {"createdBy": "personal-mcp"}
KEYRING_SERVICE = "personal-mcp-gmail"
CLIENT_SECRETS = pathlib.Path(
    os.environ.get("GOOGLE_CLIENT_SECRETS", "~/.config/personal-mcp/google_client.json")
).expanduser()


def login(account: str) -> str:
    """Interactive browser login. Run once via login.py, not from the MCP."""
    if not CLIENT_SECRETS.exists():
        raise RuntimeError(f"Google OAuth client file not found: {CLIENT_SECRETS}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    tokenstore.set(KEYRING_SERVICE, account, creds.to_json())
    r = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {creds.token}"},
    )
    r.raise_for_status()
    return r.json()["emailAddress"]


def creds(account: str, need_scope: str) -> Credentials:
    blob = tokenstore.get(KEYRING_SERVICE, account)
    if not blob:
        raise RuntimeError(f"Account '{account}' not logged in; run login.py {account}")
    # Use the scopes stored at grant time; asking to refresh with more would fail.
    c = Credentials.from_authorized_user_info(json.loads(blob))
    if need_scope not in (c.scopes or []):
        raise RuntimeError(
            f"Account '{account}' was not granted {need_scope.rsplit('/', 1)[-1]}; "
            f"run login.py {account} again"
        )
    if not c.valid:
        try:
            c.refresh(Request())
        except Exception as e:
            raise RuntimeError(f"Token refresh failed for '{account}' ({e}); run login.py {account}")
        tokenstore.set(KEYRING_SERVICE, account, c.to_json())
    return c


def request(account: str, need_scope: str, method: str, url: str, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {creds(account, need_scope).token}"}
    headers.update(kw.pop("headers", {}))
    r = httpx.request(method, url, headers=headers, timeout=30, **kw)
    r.raise_for_status()
    return r
