# personal-mcp

A private [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant (Claude) work with my own accounts from one place:

- **Personal:** Hotmail/Outlook and Gmail, Google Drive and Calendar, across several accounts at once. It can create drafts, calendar events and files, but it **cannot send email**.
- **App business:** Apple Ads, App Store Connect, Google Play Console and Applyra (ASO keyword tracking), mostly read-only, with a few narrowly guarded writes.

It runs in two modes from the same code:

- **Local (stdio)** for Claude Code and Claude Desktop, with logins kept in the macOS Keychain.
- **Remote (AWS Lambda)** so it also works from the Claude mobile app, behind owner-only OAuth.

## Tools

63 tools. The personal ones take an explicit `account` argument.

| Area | Tools |
|---|---|
| Accounts | `accounts_list` |
| Mail | `email_search`, `email_search_all` (every account at once), `email_get_message`, `email_get_conversation`, `email_create_draft` |
| Drive | `drive_search`, `drive_get_file`, `drive_create_file`, `drive_update_file`, `drive_trash_file` |
| Calendar | `calendar_list_events`, `calendar_get_event`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event` |
| Apple Ads | `ads_campaigns`, `ads_ad_groups`, `ads_keywords`, `ads_negative_keywords`, `ads_report` (keywords, search terms, ad groups), and the two-step keyword edit `ads_plan_changes` then `ads_apply_plan` |
| App Store Connect | `asc_apps`, `asc_sales` (downloads and proceeds, any date range), `asc_analytics` (impressions, page views, usage, retention), `asc_subscriptions` (trials, conversions, renewals, cancellations), `asc_ratings`, `asc_testflight`, `asc_reviews` |
| Google Play | `play_apps`, `play_installs`, `play_ratings`, `play_reviews`, `play_earnings`, `play_sales`, `play_vitals` (crashes and ANRs), `play_releases` |
| Applyra (ASO) | 25 `applyra_*` tools: tracked apps, keywords and rank history, keyword inspection, ASO health audit, listing metadata check and simulation, competitors, autocomplete, niche analysis, top charts, plan usage. Ported from the official `@applyra/mcp-server` |

Providers: Hotmail/Outlook through Microsoft Graph, Gmail/Drive/Calendar through the Google APIs, Apple Ads Platform API, App Store Connect API, Google Play Developer, Reporting and Cloud Storage APIs, and the Applyra REST API.

## Design notes

- **No sending, by design.** Only drafts are created. Drive and Calendar writes are restricted to items the server created itself (tagged `createdBy`), Drive only moves files to Trash, and calendar calls never send invitations.
- **Apple Ads edits are two-step and narrow.** Only keywords and negative keywords can change, never budgets, campaigns or ads. `ads_plan_changes` validates and previews and changes nothing; `ads_apply_plan` applies only an unmodified, signed plan, within 15 minutes, and only when the user has explicitly approved it. Bids are capped (`ASA_MAX_BID`, and at most +25% per change), with at most 50 changes per plan.
- **App Store Connect and Google Play are read-only.** The one exception is that the first `asc_analytics` call for an app switches on Apple's analytics report request, since Apple generates no data until one exists. Play never replies to reviews or touches releases or listings.
- **Applyra writes stay inside the Applyra workspace** (tracking keywords, favorites, competitors), and some tools use plan quota; their descriptions say so.
- **Untrusted content.** Mail, documents, events, app reviews and ad search terms are treated as untrusted input; tool descriptions tell the model not to follow instructions found inside them, and bodies are truncated.
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

The app-business tools are optional and each is configured through environment variables (SSM parameters in remote mode). Without them, the matching tools return a "not configured" error:

| Integration | Variables |
|---|---|
| Apple Ads | `ASA_CLIENT_ID`, `ASA_TEAM_ID`, `ASA_KEY_ID`, `ASA_PRIVATE_KEY` or `ASA_PRIVATE_KEY_PATH`, optional `ASA_AD_ACCOUNT_ID`, `ASA_MAX_BID` |
| App Store Connect | `ASC_ISSUER_ID`, `ASC_KEY_ID`, `ASC_PRIVATE_KEY` or `ASC_PRIVATE_KEY_PATH`, `ASC_VENDOR_NUMBER` (for sales and subscription reports) |
| Google Play | a service account (`PLAY_SERVICE_ACCOUNT` or `PLAY_SERVICE_ACCOUNT_PATH`) invited in Play Console, `PLAY_REPORTS_BUCKET` (the `pubsite_prod_rev_...` bucket), `PLAY_APPS="alias=com.example.app,..."` |
| Applyra | `APPLYRA_API_KEY` |

Accounts are listed in `accounts.json` (not committed), for example `{"hotmail": "outlook", "work": "gmail"}`. Google logins need an OAuth desktop client JSON at `~/.config/personal-mcp/google_client.json`.

## Status

Personal project. The Google Play report parsing follows Google's documentation but has not been checked against every report type. There is no automated test suite; the OAuth flow was checked with a local script and the deployment was verified by hand. Nothing here is audited, so review it before trusting it with your own accounts.

## License

MIT, see [LICENSE](LICENSE).
