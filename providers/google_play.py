"""Google Play Console client: reviews, installs, ratings, earnings, sales, vitals and releases.

Auth is a service account invited in Play Console (Users and permissions) with "View app information
and download bulk reports" and "View financial data". Read-only by design: no review replies, no
releases, no listing or price changes. The only non-GET calls are (a) Play Developer Reporting
`:query` POSTs, which only read, and (b) play_releases opening a throwaway edit to read tracks and
deleting it again without committing. Installs, ratings, older reviews, earnings and sales have no
API: they are CSV/ZIP reports in the developer's Cloud Storage bucket (pubsite_prod_rev_<id>).
Report column names follow Google's documentation and are matched case-insensitively; they were not
verified against real files when this was written."""
import csv
import datetime as dt
import io
import json
import os
import pathlib
import threading
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

PUBLISHER = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
REPORTING = "https://playdeveloperreporting.googleapis.com/v1beta1/apps"
STORAGE = "https://storage.googleapis.com/storage/v1/b"
SCOPES = ["https://www.googleapis.com/auth/androidpublisher",
          "https://www.googleapis.com/auth/playdeveloperreporting",
          "https://www.googleapis.com/auth/devstorage.read_only"]
# alias -> package name; override with PLAY_APPS="alias=package,alias2=package2".
DEFAULT_APPS: dict[str, str] = {}
MAX_MONTHS = 13
MAX_DAYS = 400
ROW_LIMIT = 500
WORKERS = 8
TEXT_LIMIT = 1000

_creds = {"value": None}
_lock = threading.Lock()


class PlayError(RuntimeError):
    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set (Google Play is not configured).")
    return v


def _service_account() -> dict:
    inline = os.environ.get("PLAY_SERVICE_ACCOUNT")
    text = inline if inline else pathlib.Path(_env("PLAY_SERVICE_ACCOUNT_PATH")).expanduser().read_text()
    return json.loads(text)


def _token() -> str:
    with _lock:
        if _creds["value"] is None:
            _creds["value"] = service_account.Credentials.from_service_account_info(
                _service_account(), scopes=SCOPES)
        c = _creds["value"]
        if not c.valid:
            c.refresh(Request())
        return c.token


def _req(method: str, url: str, **kw) -> httpx.Response:
    r = httpx.request(method, url, timeout=60, headers={"Authorization": f"Bearer {_token()}"}, **kw)
    if r.status_code >= 300:
        raise PlayError(f"Google Play {method} {url.split('?')[0]} -> {r.status_code}: {r.text[:500]}",
                        r.status_code)
    return r


def _run(fn, jobs: list) -> list:
    _token()  # refresh once before threads share it
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, jobs))


# ---------------------------------------------------------------- apps and dates

def app_map() -> dict[str, str]:
    spec = os.environ.get("PLAY_APPS")
    if not spec:
        return dict(DEFAULT_APPS)
    return {k.strip(): v.strip() for k, v in (p.split("=", 1) for p in spec.split(",") if "=" in p)}


def apps() -> list[dict]:
    return [{"app": k, "package": v} for k, v in app_map().items()]


def _pkg(app: str) -> str:
    m = app_map()
    if app in m:
        return m[app]
    if app in m.values():
        return app
    raise ValueError(f"Unknown app '{app}'. Known: {m}")


def _day(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ValueError(f"'{s}' is not a valid YYYY-MM-DD date")


def _range(start: str | None, end: str | None, default_days: int) -> tuple[dt.date, dt.date]:
    e = _day(end) if end else dt.date.today()
    s = _day(start) if start else e - dt.timedelta(days=default_days - 1)
    if s > e:
        raise ValueError("start must not be after end")
    if (e - s).days >= MAX_DAYS:
        raise ValueError(f"Range too long (max {MAX_DAYS} days)")
    return s, e


def _months(s: dt.date, e: dt.date) -> list[str]:
    out, y, m = [], s.year, s.month
    while (y, m) <= (e.year, e.month):
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if len(out) > MAX_MONTHS:
        raise ValueError(f"{len(out)} months needed (max {MAX_MONTHS}); narrow the range")
    return out


def _month_range(start_month: str, end_month: str | None) -> tuple[dt.date, dt.date]:
    try:
        s = dt.date.fromisoformat(start_month + "-01")
        e = dt.date.fromisoformat((end_month or start_month) + "-01")
    except ValueError:
        raise ValueError("months must be YYYY-MM")
    if s > e:
        raise ValueError("start_month must not be after end_month")
    return s, e


def _num(v) -> Decimal:
    try:
        return Decimal(str(v).replace(",", "").strip() or 0)
    except InvalidOperation:
        return Decimal(0)


def _out(v: Decimal):
    return int(v) if v == v.to_integral() else float(round(v, 4))


# ---------------------------------------------------------------- Cloud Storage reports

def _bucket() -> str:
    b = _env("PLAY_REPORTS_BUCKET").strip()
    return b.removeprefix("gs://").split("/", 1)[0]


def _list(prefix: str) -> list[str]:
    names, token = [], None
    while True:
        params = {"prefix": prefix, "fields": "items(name),nextPageToken"}
        if token:
            params["pageToken"] = token
        body = _req("GET", f"{STORAGE}/{_bucket()}/o", params=params).json()
        names += [i["name"] for i in body.get("items", [])]
        token = body.get("nextPageToken")
        if not token:
            return names


def _download(name: str) -> bytes | None:
    try:
        return _req("GET", f"{STORAGE}/{_bucket()}/o/{quote(name, safe='')}",
                    params={"alt": "media"}).content
    except PlayError as ex:
        if ex.status == 404:
            return None
        raise


def _decode(data: bytes) -> str:
    # Statistics and review CSVs are UTF-16; financial ones are UTF-8.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _rows(data: bytes) -> list[dict]:
    if data[:2] == b"PK":
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.lower().endswith(".csv"):
                    out += list(csv.DictReader(io.StringIO(_decode(z.read(n)))))
        return out
    return list(csv.DictReader(io.StringIO(_decode(data))))


def _col(row: dict, *names: str) -> str:
    """Value of the first matching column (case-insensitive exact, then substring)."""
    low = {k.lower().strip(): k for k in row if k}
    for n in names:
        if n.lower() in low:
            return row[low[n.lower()]] or ""
    for n in names:
        for lk, k in low.items():
            if n.lower() in lk:
                return row[k] or ""
    return ""


def _monthly_files(names: list[str]) -> tuple[list[tuple[str, list[dict]]], list[str]]:
    """Download (name, rows) for each file; missing names are returned separately."""
    data = _run(_download, names)
    got = [(n, _rows(d)) for n, d in zip(names, data) if d is not None]
    return got, [n for n, d in zip(names, data) if d is None]


# ---------------------------------------------------------------- installs and ratings

_DIM_FILES = ("overview", "country", "app_version", "device", "os_version", "language", "carrier")


def installs(app: str, start: str | None = None, end: str | None = None, dimension: str = "overview",
             group: str = "total") -> dict:
    """Install statistics from stats/installs CSVs. Columns with "Daily" or "events" are summed over
    the range; "Total"/"Active" columns are snapshots, taken from the last day in the range."""
    pkg, dimension, group = _pkg(app), dimension.lower(), group.lower()
    if dimension not in _DIM_FILES:
        raise ValueError(f"dimension must be one of {_DIM_FILES}")
    if group not in ("total", "day", "month"):
        raise ValueError("group must be total, day or month")
    s, e = _range(start, end, 30)
    names = [f"stats/installs/installs_{pkg}_{m}_{dimension}.csv" for m in _months(s, e)]
    files, missing = _monthly_files(names)
    flows: dict = defaultdict(lambda: defaultdict(Decimal))
    snaps: dict = defaultdict(lambda: defaultdict(Decimal))
    snap_day: dict = {}
    last_day = ""
    for _, rows in files:
        for r in rows:
            day = _col(r, "Date")[:10]
            if not (s.isoformat() <= day <= e.isoformat()):
                continue
            cols = [c for c in r if c]
            dim = r[cols[2]] if dimension != "overview" and len(cols) > 2 else ""
            period = day if group == "day" else day[:7] if group == "month" else ""
            key = (period, dim)
            for c in cols:
                lc = c.lower()
                if "daily" in lc or lc.endswith("events"):
                    flows[key][c] += _num(r[c])
                elif lc.startswith(("total", "active")):
                    # Snapshot: keep the latest day's value per key.
                    if day > snap_day.get((key, c), ""):
                        snap_day[(key, c)] = day
                        snaps[key][c] = _num(r[c])
            last_day = max(last_day, day)
    keys = sorted(set(flows) | set(snaps),
                  key=lambda k: (k[0], -sum(flows[k].values())))
    out = [{**({"period": k[0]} if k[0] else {}), **({dimension: k[1]} if k[1] else {}),
            **{c: _out(v) for c, v in flows[k].items()}, **{c: _out(v) for c, v in snaps[k].items()}}
           for k in keys]
    return {"app": app, "package": pkg, "start": s.isoformat(), "end": e.isoformat(),
            "dimension": dimension, "latest_day_with_data": last_day or None, "rows": out[:ROW_LIMIT],
            "truncated": len(out) > ROW_LIMIT, "missing_files": missing,
            "note": "Daily*/events columns are summed over the range; Total*/Active* are the value on "
                    "the last day. Stats usually lag 1-2 days."}


def ratings(app: str, start: str | None = None, end: str | None = None) -> dict:
    """Daily and all-time average star rating from stats/ratings CSVs."""
    pkg = _pkg(app)
    s, e = _range(start, end, 30)
    names = [f"stats/ratings/ratings_{pkg}_{m}_overview.csv" for m in _months(s, e)]
    files, missing = _monthly_files(names)
    days = []
    for _, rows in files:
        for r in rows:
            day = _col(r, "Date")[:10]
            if s.isoformat() <= day <= e.isoformat():
                days.append({"date": day, "daily_average": _col(r, "Daily Average Rating"),
                             "total_average": _col(r, "Total Average Rating")})
    days.sort(key=lambda d: d["date"])
    rated = [d for d in days if d["daily_average"] not in ("", "NA")]
    return {"app": app, "package": pkg, "start": s.isoformat(), "end": e.isoformat(),
            "total_average": days[-1]["total_average"] if days else None,
            "days_with_new_ratings": rated, "missing_files": missing}


# ---------------------------------------------------------------- reviews

def _review_api(pkg: str, limit: int) -> list[dict]:
    out, token = [], None
    while len(out) < limit:
        params = {"maxResults": min(100, limit)}
        if token:
            params["token"] = token
        body = _req("GET", f"{PUBLISHER}/{pkg}/reviews", params=params).json()
        for rv in body.get("reviews", []):
            user = next((c["userComment"] for c in rv.get("comments", []) if "userComment" in c), {})
            dev = next((c["developerComment"] for c in rv.get("comments", []) if "developerComment" in c),
                       None)
            secs = int(user.get("lastModified", {}).get("seconds", 0))
            out.append({"id": rv.get("reviewId"), "rating": user.get("starRating"),
                        "text": (user.get("text") or "").strip()[:TEXT_LIMIT],
                        "date": dt.datetime.fromtimestamp(secs, dt.timezone.utc).isoformat() if secs else None,
                        "language": user.get("reviewerLanguage"), "app_version": user.get("appVersionName"),
                        "device": user.get("device"), "replied": dev is not None})
        token = body.get("tokenPagination", {}).get("nextPageToken")
        if not token:
            break
    return out


def _review_csv(pkg: str, s: dt.date, e: dt.date) -> tuple[list[dict], list[str]]:
    files, missing = _monthly_files([f"reviews/reviews_{pkg}_{m}.csv" for m in _months(s, e)])
    out = []
    for _, rows in files:
        for r in rows:
            when = _col(r, "Review Submit Date and Time")
            if not (s.isoformat() <= when[:10] <= e.isoformat()):
                continue
            title, text = _col(r, "Review Title"), _col(r, "Review Text")
            out.append({"rating": int(_num(_col(r, "Star Rating"))) or None,
                        "text": " - ".join(x for x in (title, text) if x).strip()[:TEXT_LIMIT],
                        "date": when, "language": _col(r, "Reviewer Language"),
                        "app_version": _col(r, "App Version Name"), "device": _col(r, "Device"),
                        "replied": bool(_col(r, "Developer Reply Text")), "link": _col(r, "Review Link")})
    return out, missing


def reviews(app: str, limit: int = 20, rating: int | None = None, start: str | None = None,
            end: str | None = None) -> dict:
    """Without start: the API, which only returns reviews created or edited in the last 7 days.
    With start (YYYY-MM-DD): the monthly review CSVs in the reports bucket."""
    pkg = _pkg(app)
    if start:
        s, e = _range(start, end, 1)
        rows, missing = _review_csv(pkg, s, e)
        source = "reports bucket"
    else:
        rows, missing, source = _review_api(pkg, 200 if rating else limit), [], "api (last 7 days only)"
    if rating:
        rows = [r for r in rows if r["rating"] == rating]
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return {"app": app, "package": pkg, "source": source, "reviews": rows[:limit],
            "count_in_range": len(rows), "missing_files": missing}


# ---------------------------------------------------------------- earnings and sales

_EARN_DIMS = {"app": ("Product id", "Package"), "type": ("Transaction Type",),
              "country": ("Buyer Country",), "sku": ("Sku Id",), "product_type": ("Product Type",)}


def earnings(start_month: str, end_month: str | None = None, app: str | None = None,
             group_by: str = "app,type") -> dict:
    """Net earnings from the monthly earnings reports (merchant currency), summed by group_by."""
    pkg = _pkg(app) if app else None
    dims = [g.strip().lower() for g in group_by.split(",") if g.strip()]
    for g in dims:
        if g not in _EARN_DIMS and g != "month":
            raise ValueError(f"group_by values must be among {sorted(_EARN_DIMS) + ['month']}")
    s, e = _month_range(start_month, end_month)
    months = _months(s, e)
    listed = _run(lambda m: _list(f"earnings/earnings_{m}"), months)
    names = [n for ns in listed for n in ns]
    files, _ = _monthly_files(names)
    agg: dict = defaultdict(lambda: defaultdict(Decimal))
    net: dict = defaultdict(lambda: defaultdict(Decimal))
    for name, rows in files:
        month = next((m for m in months if f"earnings_{m}" in name), "")
        for r in rows:
            product = _col(r, *_EARN_DIMS["app"])
            if pkg and product != pkg:
                continue
            cur = _col(r, "Merchant Currency")
            amount = _num(_col(r, "Amount (Merchant Currency)"))
            key = tuple(f"{month[:4]}-{month[4:]}" if g == "month" else _col(r, *_EARN_DIMS[g])
                        for g in dims)
            agg[key][cur] += amount
            net[product][cur] += amount
    found = sorted({m for m in months for n in names if f"earnings_{m}" in n})
    rows = [{**dict(zip(dims, k)), "amount": {c: f"{v:.2f}" for c, v in cur.items()}}
            for k, cur in sorted(agg.items())]
    return {"start_month": start_month, "end_month": end_month or start_month,
            "net_by_app": {p: {c: f"{v:.2f}" for c, v in cur.items()} for p, cur in sorted(net.items())},
            "rows": rows[:ROW_LIMIT], "truncated": len(rows) > ROW_LIMIT,
            "months_found": [f"{m[:4]}-{m[4:]}" for m in found],
            "months_missing": [f"{m[:4]}-{m[4:]}" for m in months if m not in found],
            "note": "Net of Google fees, taxes and refunds (rows by Transaction Type show the split). A "
                    "month's earnings report appears early in the following month; use play_sales for "
                    "the current month."}


def sales(start: str | None = None, end: str | None = None, app: str | None = None,
          by_country: bool = False) -> dict:
    """Orders from the sales reports (buyer currency, gross, updated daily)."""
    pkg = _pkg(app) if app else None
    s, e = _range(start, end, 30)
    names = [f"sales/salesreport_{m}.zip" for m in _months(s, e)]
    files, missing = _monthly_files(names)
    agg: dict = defaultdict(lambda: {"orders": 0, "charged": defaultdict(Decimal)})
    for _, rows in files:
        for r in rows:
            day = _col(r, "Order Charged Date")[:10]
            product = _col(r, "Product ID")
            if not (s.isoformat() <= day <= e.isoformat()) or (pkg and product != pkg):
                continue
            key = (product, _col(r, "SKU ID"), _col(r, "Financial Status")) + (
                (_col(r, "Country of Buyer"),) if by_country else ())
            a = agg[key]
            a["orders"] += 1
            a["charged"][_col(r, "Currency of Sale")] += _num(_col(r, "Charged Amount"))
    cols = ("package", "sku", "status") + (("country",) if by_country else ())
    rows = [{**dict(zip(cols, k)), "orders": v["orders"],
             "charged": {c: f"{x:.2f}" for c, x in v["charged"].items()}} for k, v in sorted(agg.items())]
    return {"start": s.isoformat(), "end": e.isoformat(), "rows": rows[:ROW_LIMIT],
            "truncated": len(rows) > ROW_LIMIT, "missing_files": missing,
            "note": "Gross amounts charged to buyers incl. tax, before Google's fee; see play_earnings "
                    "for net payouts."}


# ---------------------------------------------------------------- vitals

def _date(d: dt.date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day, "timeZone": {"id": "America/Los_Angeles"}}


def _latest(pkg: str, metric_set: str) -> dt.date | None:
    body = _req("GET", f"{REPORTING}/{pkg}/{metric_set}").json()
    for f in body.get("freshnessInfo", {}).get("freshnesses", []):
        if f.get("aggregationPeriod") == "DAILY":
            t = f["latestEndTime"]
            return dt.date(t["year"], t["month"], t["day"]) - dt.timedelta(days=1)  # end is exclusive
    return None


def _metric_rows(pkg: str, metric_set: str, metrics: list[str], s: dt.date, e: dt.date) -> list[dict]:
    body = _req("POST", f"{REPORTING}/{pkg}/{metric_set}:query", json={
        "timelineSpec": {"aggregationPeriod": "DAILY", "startTime": _date(s),
                         "endTime": _date(e + dt.timedelta(days=1))},
        "metrics": metrics, "pageSize": 400}).json()
    out = []
    for r in body.get("rows", []):
        t = r.get("startTime", {})
        row = {"date": f"{t.get('year')}-{t.get('month', 0):02d}-{t.get('day', 0):02d}"}
        for m in r.get("metrics", []):
            v = m.get("decimalValue", {}).get("value")
            row[m["metric"]] = float(v) if v is not None else None
        out.append(row)
    return sorted(out, key=lambda r: r["date"])


def _avg(rows: list[dict], metric: str) -> float | None:
    """Mean of a daily rate weighted by that day's distinctUsers."""
    pairs = [(r[metric], r.get("distinctUsers") or 0) for r in rows if r.get(metric) is not None]
    users = sum(u for _, u in pairs)
    if not pairs:
        return None
    return round(sum(v * u for v, u in pairs) / users if users else sum(v for v, _ in pairs) / len(pairs), 6)


def vitals(app: str, start: str | None = None, end: str | None = None) -> dict:
    """Daily crash and ANR rates (Android vitals) plus range averages weighted by users."""
    pkg = _pkg(app)
    s, e = _range(start, end, 28)
    out = {"app": app, "package": pkg, "start": s.isoformat()}
    specs = {"crash": ("crashRateMetricSet", ["crashRate", "userPerceivedCrashRate", "distinctUsers"]),
             "anr": ("anrRateMetricSet", ["anrRate", "userPerceivedAnrRate", "distinctUsers"])}

    def one(kind):
        metric_set, metrics = specs[kind]
        latest = _latest(pkg, metric_set)
        end_d = min(e, latest) if latest else e
        if end_d < s:
            return kind, {"rows": [], "latest_day": latest and latest.isoformat()}
        rows = _metric_rows(pkg, metric_set, metrics, s, end_d)
        if not rows:
            return kind, {"latest_day": end_d.isoformat(), "rows": [],
                          "message": "Google returned no data: usually too few active users for vitals."}
        return kind, {"latest_day": end_d.isoformat(), "rows": rows,
                      "average": {m: _avg(rows, m) for m in metrics if m != "distinctUsers"}}

    out.update(dict(_run(one, list(specs))))
    out["note"] = ("Rates are fractions (0.0109 = 1.09%). Google's bad-behaviour thresholds: "
                   "user-perceived crash rate 1.09%, user-perceived ANR rate 0.47%. Small apps can "
                   "have days without data.")
    return out


# ---------------------------------------------------------------- releases

def releases(app: str) -> dict:
    """Tracks and their releases. Reading tracks needs an edit: one is opened and always deleted,
    never committed, so nothing changes in Play Console."""
    pkg = _pkg(app)
    edit = _req("POST", f"{PUBLISHER}/{pkg}/edits", json={}).json()["id"]
    try:
        tracks = _req("GET", f"{PUBLISHER}/{pkg}/edits/{edit}/tracks").json().get("tracks", [])
    finally:
        _req("DELETE", f"{PUBLISHER}/{pkg}/edits/{edit}")
    out = []
    for t in tracks:
        out.append({"track": t.get("track"), "releases": [
            {"name": r.get("name"), "status": r.get("status"), "version_codes": r.get("versionCodes", []),
             "user_fraction": r.get("userFraction"),
             "notes": {n.get("language"): (n.get("text") or "")[:300] for n in r.get("releaseNotes", [])}}
            for r in t.get("releases", [])]})
    return {"app": app, "package": pkg, "tracks": out}
