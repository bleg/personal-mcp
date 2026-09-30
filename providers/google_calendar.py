"""Google Calendar (calendar.events).

Writes are limited to events this MCP created (tagged in extendedProperties.private),
and invitations are never sent (sendUpdates=none).
"""
import datetime

from providers import google_auth

API = "https://www.googleapis.com/calendar/v3/calendars"


def _rfc3339(value: str) -> str:
    """Accept ISO 8601; a value without a timezone is taken as the machine's local time."""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def _summary(e: dict) -> dict:
    start, end = e.get("start", {}), e.get("end", {})
    return {
        "id": e["id"],
        "title": e.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": e.get("location"),
        "attendees": [a["email"] for a in e.get("attendees", [])],
        "link": e.get("htmlLink"),
    }


def list_events(account: str, start: str, end: str, calendar_id: str = "primary", limit: int = 50) -> list[dict]:
    r = google_auth.request(
        account, google_auth.CALENDAR_EVENTS, "GET", f"{API}/{calendar_id}/events",
        params={
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(limit, 250),
        },
    ).json()
    return [_summary(e) for e in r.get("items", [])]


def get_event(account: str, event_id: str, calendar_id: str = "primary") -> dict:
    e = google_auth.request(
        account, google_auth.CALENDAR_EVENTS, "GET", f"{API}/{calendar_id}/events/{event_id}"
    ).json()
    out = _summary(e)
    out["description"] = (e.get("description") or "")[:20000]
    out["organizer"] = (e.get("organizer") or {}).get("email")
    return out


def _time(value: str) -> dict:
    """A bare date (YYYY-MM-DD) makes an all-day event; otherwise a timed one."""
    return {"date": value} if len(value) == 10 else {"dateTime": _rfc3339(value)}


def _url(calendar_id: str, event_id: str = "") -> str:
    return f"{API}/{calendar_id}/events" + (f"/{event_id}" if event_id else "")


def _call(account: str, method: str, url: str, **kw):
    return google_auth.request(
        account, google_auth.CALENDAR_EVENTS, method, url, params={"sendUpdates": "none"}, **kw
    )


def _require_ours(account: str, event_id: str, calendar_id: str) -> None:
    e = _call(account, "GET", _url(calendar_id, event_id)).json()
    tag = (e.get("extendedProperties") or {}).get("private", {})
    if tag.get("createdBy") != google_auth.CREATED_BY["createdBy"]:
        raise PermissionError("Refusing: this event was not created by personal-mcp.")


def create_event(
    account: str, title: str, start: str, end: str, description: str = "",
    location: str = "", attendees: list[str] | None = None, calendar_id: str = "primary",
) -> dict:
    body = {
        "summary": title,
        "start": _time(start),
        "end": _time(end),
        "description": description,
        "location": location,
        "attendees": [{"email": a} for a in attendees or []],
        "extendedProperties": {"private": google_auth.CREATED_BY},
    }
    e = _call(account, "POST", _url(calendar_id), json=body).json()
    return {**_summary(e), "status": "created; no invitations sent"}


def update_event(
    account: str, event_id: str, title: str | None = None, start: str | None = None,
    end: str | None = None, description: str | None = None, location: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    _require_ours(account, event_id, calendar_id)
    body = {}
    if title is not None: body["summary"] = title
    if start is not None: body["start"] = _time(start)
    if end is not None: body["end"] = _time(end)
    if description is not None: body["description"] = description
    if location is not None: body["location"] = location
    e = _call(account, "PATCH", _url(calendar_id, event_id), json=body).json()
    return {**_summary(e), "status": "updated; no invitations sent"}


def delete_event(account: str, event_id: str, calendar_id: str = "primary") -> dict:
    _require_ours(account, event_id, calendar_id)
    _call(account, "DELETE", _url(calendar_id, event_id))
    return {"id": event_id, "status": "deleted"}
