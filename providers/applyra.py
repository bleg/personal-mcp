"""Applyra (ASO keyword tracking) REST client, ported from the official @applyra/mcp-server 1.5.0.

Every call goes to https://www.applyra.io/api/v1 with the X-API-Key header. Some tools change
data inside the Applyra workspace only (track/untrack keywords, favorites, apps, competitors) or
use plan quota (inspect, autocomplete, niche analysis, metadata simulation); nothing here touches
App Store Connect or Play Console."""
import os

import httpx

BASE_URL = os.environ.get("APPLYRA_BASE_URL", "https://www.applyra.io")


def _key() -> str:
    v = os.environ.get("APPLYRA_API_KEY")
    if not v:
        raise RuntimeError("APPLYRA_API_KEY is not set (Applyra is not configured).")
    return v


def _error(body, fallback: str) -> str:
    """The API's headline plus the field-level details it puts in `details`."""
    if not isinstance(body, dict):
        return fallback
    parts = [body.get("error") or fallback]
    details = body.get("details") or {}
    issues = details.get("issues") or {}
    if issues.get("formErrors"):
        parts.append("; ".join(issues["formErrors"]))
    for field, messages in (issues.get("fieldErrors") or {}).items():
        if messages:
            parts.append(f"{field}: {'; '.join(messages)}")
    if details.get("notes"):
        parts.append(" ".join(details["notes"]))
    return " | ".join(parts)


def call(path: str, params: dict | None = None, method: str = "GET", body: dict | None = None):
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if body is not None:
        body = {k: v for k, v in body.items() if v is not None}
    r = httpx.request(method, f"{BASE_URL}/api/v1{path}", params=params or None, json=body,
                      headers={"X-API-Key": _key()}, timeout=80)  # under the 90 s Lambda limit
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Applyra API error ({r.status_code}): not JSON: {r.text[:200]}")
    if r.status_code >= 300:
        raise RuntimeError(f"Applyra API error ({r.status_code}): {_error(data, r.reason_phrase)}")
    return data
