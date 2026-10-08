"""Apple Ads Platform API client (api.ads.apple.com/v1): reports plus guarded keyword edits.

Scope is deliberately narrow: keywords and negative keywords only. No campaign, ad group,
budget or creative changes. Edits are two-step (plan, then apply a signed plan)."""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import time
from decimal import Decimal

import httpx
import jwt

API = "https://api.ads.apple.com/v1"
TOKEN_URL = "https://appleid.apple.com/auth/oauth2/token"

_token = {"value": "", "exp": 0.0}
_account: dict = {}


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set (Apple Ads is not configured).")
    return v


def private_key() -> str:
    inline = os.environ.get("ASA_PRIVATE_KEY")
    if inline:
        return inline.replace("\\n", "\n")
    return pathlib.Path(_env("ASA_PRIVATE_KEY_PATH")).read_text()


def _client_secret() -> str:
    now = int(time.time())
    claims = {"iss": _env("ASA_TEAM_ID"), "sub": _env("ASA_CLIENT_ID"),
              "aud": "https://appleid.apple.com", "iat": now, "exp": now + 3600}
    return jwt.encode(claims, private_key(), algorithm="ES256", headers={"kid": _env("ASA_KEY_ID")})


def _access_token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = httpx.post(TOKEN_URL, timeout=30, data={
        "grant_type": "client_credentials", "client_id": _env("ASA_CLIENT_ID"),
        "client_secret": _client_secret(), "scope": "searchadsorg"})
    if r.status_code != 200:
        raise RuntimeError(f"Apple Ads token request failed ({r.status_code}): {r.text[:300]}")
    body = r.json()
    _token.update(value=body["access_token"], exp=time.time() + int(body.get("expires_in", 3600)))
    return _token["value"]


def call(method: str, path: str, body: dict | None = None, scoped: bool = True) -> dict:
    headers = {"Authorization": f"Bearer {_access_token()}"}
    if scoped:
        headers["X-AP-Context"] = f"adAccountId={ad_account_id()}"
    r = httpx.request(method, API + path, json=body, headers=headers, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Apple Ads {method} {path} -> {r.status_code}: {r.text[:600]}")
    return r.json() if r.content else {}


def ad_account_id() -> str:
    """ASA_AD_ACCOUNT_ID if set, else the caller's only ad account (via GET /acls)."""
    if os.environ.get("ASA_AD_ACCOUNT_ID"):
        return os.environ["ASA_AD_ACCOUNT_ID"]
    if "id" not in _account:
        acls = call("GET", "/acls", scoped=False)["result"]["acls"]
        if len(acls) != 1:
            raise RuntimeError(f"Set ASA_AD_ACCOUNT_ID; accessible ad accounts: {acls}")
        _account["id"] = str(acls[0]["adAccount"]["id"])
    return _account["id"]


# ---- reads -----------------------------------------------------------------------------

def _query(path: str, filters: list | None = None, page: int = 500) -> list:
    out, offset = [], 0
    while True:
        body: dict = {"pagination": {"offset": offset, "pageSize": page}}
        if filters:
            body["filters"] = filters
        rows = call("POST", path, body)["result"]
        out += rows
        if len(rows) < page:
            return out
        offset += page


def _eq(field: str, value) -> dict:
    return {"field": field, "operator": "EQUALS", "value": value}


def campaigns() -> list[dict]:
    return [{"id": c["id"], "name": c["name"], "status": c["status"], "state": c["displayStatus"],
             "app_id": c.get("promotedObjectId"), "daily_budget": c.get("dailyBudget", {}).get("value"),
             "countries": c.get("targeting", {}).get("countryOrRegion", {}).get("include")}
            for c in _query("/campaigns/query")]


def ad_groups(campaign_id: int | None = None) -> list[dict]:
    rows = _query("/adgroups/query", [_eq("campaignId", campaign_id)] if campaign_id else None)
    return [{"id": g["id"], "campaign_id": g["campaignId"], "name": g["name"], "status": g["status"],
             "default_bid": g.get("bidStrategy", {}).get("bid")} for g in rows]


def keywords(ad_group_id: int) -> list[dict]:
    return [{"id": k["id"], "text": k["text"], "match_type": k["matchType"], "status": k["status"],
             "bid": k.get("bid")} for k in _query("/keywords/query", [_eq("adGroupId", ad_group_id)])]


def negative_keywords(campaign_id: int | None = None, ad_group_id: int | None = None) -> list[dict]:
    """Campaign-level negatives (campaign_id) or ad-group-level ones (ad_group_id)."""
    if bool(campaign_id) == bool(ad_group_id):
        raise ValueError("Give exactly one of campaign_id or ad_group_id.")
    filters = ([_eq("campaignId", campaign_id), {"field": "adGroupId", "operator": "IS_NULL"}]
               if campaign_id else [_eq("adGroupId", ad_group_id)])
    return [{"id": n["id"], "text": n["text"], "match_type": n["matchType"], "status": n.get("status"),
             "campaign_id": n.get("campaignId"), "ad_group_id": n.get("adGroupId")}
            for n in _query("/negative-keywords/query", filters)]


def report(kind: str, campaign_id: int, start: str, end: str, ad_group_id: int | None = None,
           limit: int = 100) -> dict:
    """kind: keywords | searchterms | adgroups. start/end are YYYY-MM-DD in the account time zone."""
    if kind not in ("keywords", "searchterms", "adgroups"):
        raise ValueError("kind must be keywords, searchterms or adgroups")
    filters = [_eq("campaignId", campaign_id)] + ([_eq("adGroupId", ad_group_id)] if ad_group_id else [])
    body = {"timeRange": {"start": start, "end": end, "timeZone": "ORTZ"}, "filters": filters,
            "pagination": {"offset": 0, "pageSize": min(limit, 1000)},
            "options": {"includeRows": ["GRAND_TOTAL"]}}
    return call("POST", f"/reports/apps/{kind}/query", body)


# ---- guarded keyword edits ---------------------------------------------------------------
# plan() validates and previews; apply() runs only a plan this server signed (so exactly what
# was previewed), within PLAN_TTL, via the bulk endpoints with all-or-nothing semantics.

PLAN_TTL = 900
MAX_ITEMS = 50
MAX_BID_INCREASE = Decimal("0.25")  # per change, relative to the current effective bid
_STATUS = {"ENABLED": "ENABLED", "ACTIVE": "ENABLED", "PAUSED": "PAUSED"}


def _max_bid() -> Decimal:
    return Decimal(os.environ.get("ASA_MAX_BID", "3.00"))


def _currency() -> str:
    if "currency" not in _account:
        _account["currency"] = call("GET", f"/ad-accounts/{ad_account_id()}")["result"]["currency"]
    return _account["currency"]


def _money(amount) -> dict:
    d = Decimal(str(amount)).quantize(Decimal("0.01"))
    if d <= 0:
        raise ValueError(f"Bid must be positive, got {amount}.")
    return {"amount": f"{d:.2f}", "currency": _currency()}


def _sign(payload: str) -> str:
    key = hashlib.sha256(b"personal-mcp-ads-plan:" + private_key().encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def plan(add_keywords=(), update_keywords=(), delete_keywords=(), add_negative_keywords=(),
         delete_negative_keywords=(), override_caps: bool = False) -> dict:
    n = sum(map(len, (add_keywords, update_keywords, delete_keywords, add_negative_keywords,
                      delete_negative_keywords)))
    if not 0 < n <= MAX_ITEMS:
        raise ValueError(f"Between 1 and {MAX_ITEMS} changes per plan (got {n}).")
    ops, lines, problems = {}, [], []
    groups: dict = {}

    def group(gid):
        if gid not in groups:
            g = call("GET", f"/adgroups/{gid}")["result"]
            groups[gid] = (g, {(k["text"].lower(), k["match_type"]): k["id"] for k in keywords(gid)})
        return groups[gid]

    def check_bid(bid: dict, current: Decimal | None, label: str):
        new = Decimal(bid["amount"])
        if new > _max_bid() and not override_caps:
            problems.append(f"{label}: bid {new} exceeds the ceiling {_max_bid()} (ASA_MAX_BID).")
        if current and new > current * (1 + MAX_BID_INCREASE) and not override_caps:
            problems.append(f"{label}: bid {current} -> {new} is over +{MAX_BID_INCREASE:.0%} in one change.")

    creates = []
    for k in add_keywords:
        g, existing = group(k["ad_group_id"])
        mt = k.get("match_type", "EXACT").upper()
        label = f'add "{k["text"]}" [{mt}] to ad group {g["name"]}'
        if (k["text"].lower(), mt) in existing:
            problems.append(f"{label}: already exists.")
            continue
        data = {"adGroupId": k["ad_group_id"], "text": k["text"], "matchType": mt,
                "status": _STATUS[k.get("status", "ENABLED").upper()]}
        if k.get("bid") is not None:
            data["bid"] = _money(k["bid"])
            check_bid(data["bid"], None, label)
        bid_txt = data.get("bid", {}).get("amount") or f'{g["bidStrategy"]["bid"]["amount"]} (ad group default)'
        creates.append(data)
        lines.append(f"{label}, bid {bid_txt}")
    ops["add_keywords"] = creates

    updates = []
    for u in update_keywords:
        kw = call("GET", f"/keywords/{u['id']}")["result"]
        g, _ = group(kw["adGroupId"])
        cur_bid = Decimal((kw.get("bid") or g["bidStrategy"]["bid"])["amount"])
        data, bits = {"id": u["id"]}, []
        label = f'keyword "{kw["text"]}" [{kw["matchType"]}] in {g["name"]}'
        if u.get("bid") is not None:
            data["bid"] = _money(u["bid"])
            check_bid(data["bid"], cur_bid, label)
            bits.append(f'bid {cur_bid} -> {data["bid"]["amount"]}')
        if u.get("status"):
            data["status"] = _STATUS[u["status"].upper()]
            bits.append(f'status {kw["status"]} -> {data["status"]}')
        if len(data) == 1:
            problems.append(f"{label}: nothing to change.")
            continue
        updates.append(data)
        lines.append(f'update {label}: {", ".join(bits)}')
    ops["update_keywords"] = updates

    ops["delete_keywords"] = []
    for kid in delete_keywords:
        kw = call("GET", f"/keywords/{kid}")["result"]
        g, _ = group(kw["adGroupId"])
        ops["delete_keywords"].append({"id": kid})
        lines.append(f'DELETE keyword "{kw["text"]}" [{kw["matchType"]}] from {g["name"]} (soft-delete; pausing is reversible)')

    negs = []
    for k in add_negative_keywords:
        if bool(k.get("campaign_id")) == bool(k.get("ad_group_id")):
            problems.append(f'negative "{k["text"]}": give exactly one of campaign_id or ad_group_id.')
            continue
        scope = ({"campaignId": k["campaign_id"]} if k.get("campaign_id") else {"adGroupId": k["ad_group_id"]})
        mt = k.get("match_type", "EXACT").upper()
        existing = {(x["text"].lower(), x["match_type"]) for x in negative_keywords(k.get("campaign_id"), k.get("ad_group_id"))}
        if (k["text"].lower(), mt) in existing:
            problems.append(f'negative "{k["text"]}" [{mt}] already exists in that scope.')
            continue
        negs.append({**scope, "text": k["text"], "matchType": mt})
        lines.append(f'add negative "{k["text"]}" [{mt}] at {"campaign " + str(k["campaign_id"]) if k.get("campaign_id") else "ad group " + str(k["ad_group_id"])}')
    ops["add_negative_keywords"] = negs

    ops["delete_negative_keywords"] = []
    for nid in delete_negative_keywords:
        nk = call("GET", f"/negative-keywords/{nid}")["result"]
        ops["delete_negative_keywords"].append({"id": nid})
        lines.append(f'DELETE negative keyword "{nk["text"]}" [{nk["matchType"]}]')

    if problems:
        raise ValueError("Plan refused, nothing changed:\n- " + "\n- ".join(problems))
    payload = base64.urlsafe_b64encode(json.dumps(
        {"ops": ops, "exp": int(time.time()) + PLAN_TTL, "acct": ad_account_id()}).encode()).decode()
    return {"summary": lines, "expires_in_minutes": PLAN_TTL // 60,
            "plan": f"{payload}.{_sign(payload)}",
            "next": "Show this summary to the user. Only after they approve, call ads_apply_plan with the plan string."}


_BULK = [("add_keywords", "/keywords/bulk-create"), ("update_keywords", "/keywords/bulk-update"),
         ("delete_keywords", "/keywords/bulk-delete"),
         ("add_negative_keywords", "/negative-keywords/bulk-create"),
         ("delete_negative_keywords", "/negative-keywords/bulk-delete")]


def apply(signed_plan: str) -> dict:
    payload, _, sig = signed_plan.partition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        raise ValueError("Invalid plan: it was not issued by ads_plan_changes.")
    data = json.loads(base64.urlsafe_b64decode(payload))
    if time.time() > data["exp"]:
        raise ValueError("Plan expired; create a new plan and re-confirm with the user.")
    if data["acct"] != ad_account_id():
        raise ValueError("Plan belongs to a different ad account.")
    done = {}
    for name, path in _BULK:
        items = [{"correlationId": i, "data": d} for i, d in enumerate(data["ops"].get(name, []))]
        if not items:
            continue
        try:
            res = call("POST", path, {"allowPartialSuccess": False, "items": items})
        except RuntimeError as e:
            return {"applied": done, "failed_step": name, "error": str(e),
                    "note": "Earlier steps were applied; this step and later ones were not."}
        done[name] = [r.get("success") for r in res.get("result", [])]
    return {"applied": done}
