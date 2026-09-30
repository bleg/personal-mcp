# personal-mcp

A private [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant (Claude) search and read my own Hotmail/Outlook and Gmail accounts, plus Google Drive and Calendar, across several accounts at once. It can create drafts, calendar events and files, but it **cannot send email**.

It runs in two modes from the same code:

- **Local (stdio)** for Claude Code and Claude Desktop, with logins kept in the macOS Keychain.
- **Remote (AWS Lambda)** so it also works from the Claude mobile app, behind owner-only OAuth.

## Tools

16 tools, all taking an explicit `account` argument:

| Area | Tools |
|---|---|
| Accounts | `accounts_list` |
| Mail | `email_search`, `email_search_all` (every account at once), `email_get_message`, `email_get_conversation`, `email_create_draft` |
| Drive | `drive_search`, `drive_get_file`, `drive_create_file`, `drive_update_file`, `drive_trash_file` |
| Calendar | `calendar_list_events`, `calendar_get_event`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event` |

Providers: Hotmail/Outlook through Microsoft Graph, Gmail/Drive/Calendar through the Google APIs.

## Design notes

- **No sending, by design.** Only drafts are created. Drive and Calendar writes are restricted to items the server created itself (tagged `createdBy`), Drive only moves files to Trash, and calendar calls never send invitations.
- **Untrusted content.** Mail, documents and events are treated as untrusted input; tool descriptions tell the model not to follow instructions found inside them, and bodies are truncated.
- **Audit log** of every operation (account, operation, query or IDs; never bodies or tokens).
- **One code path for credentials** (`tokenstore.py`): Keychain locally, a private S3 bucket on AWS.

## Remote mode

```
Claude app -> Lambda Function URL (HTTPS) -> Lambda (FastMCP, streamable HTTP, stateless)
                                                 |-- private S3 bucket (logins, OAuth state)
                                                 '-- SSM Parameter Store (secrets/config)
```

- **OAuth 2.1 authorization server** in `oauth_provider.py`: dynamic client registration, PKCE, single-use authorization codes, rotating refresh tokens, tokens stored as SHA-256 hashes.
- **Owner-only sign-in** through Google, checked against an allowlisted email; redirect URIs are limited to Claude's own callback; requests with a wrong Host header are rejected.
- The server refuses to start without its OAuth configuration, so it can never be exposed unauthenticated.
- Runs on the AWS free tier; a reserved-concurrency cap limits abuse and setting it to 0 switches the server off.

Deployment (AWS SAM) is described in [deploy/README.md](deploy/README.md).

## Local setup

```
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
MS_CLIENT_ID=<entra app id> .venv/bin/python login.py <account>   # one browser login per account
MS_CLIENT_ID=<entra app id> .venv/bin/python server.py
```

Accounts are listed in `accounts.json` (not committed), for example `{"hotmail": "outlook", "work": "gmail"}`. Google logins need an OAuth desktop client JSON at `~/.config/personal-mcp/google_client.json`.

## Status

Personal project. There is no automated test suite; the OAuth flow was checked with a local script and the deployment was verified by hand. Nothing here is audited, so review it before trusting it with your own accounts.

## License

MIT, see [LICENSE](LICENSE).
