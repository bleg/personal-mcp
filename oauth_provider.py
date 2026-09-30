"""OAuth 2.1 authorization server for the remote MCP endpoint, owner-only.

claude.ai registers itself (dynamic client registration) and sends the user to /authorize.
We hand off to Sign in with Google and issue a code only if the verified Google email is
OWNER_EMAIL. All state is stored via tokenstore (S3 on AWS); tokens are stored by SHA-256
so a leaked bucket listing does not yield usable tokens.

Env: MCP_PUBLIC_URL (base URL, no trailing slash), OWNER_EMAIL, GOOGLE_WEB_CLIENT_ID,
GOOGLE_WEB_CLIENT_SECRET, optional ALLOWED_REDIRECT_PREFIXES (comma separated).
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

import tokenstore

SCOPE = "mcp"
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
PENDING_TTL = 600
DEFAULT_REDIRECTS = "https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _put(service: str, key: str, obj) -> None:
    tokenstore.set(service, key, json.dumps(obj))


def _get(service: str, key: str):
    blob = tokenstore.get(service, key)
    return json.loads(blob) if blob else None


class OwnerOAuthProvider:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.redirects = [
            r.strip()
            for r in os.environ.get("ALLOWED_REDIRECT_PREFIXES", DEFAULT_REDIRECTS).split(",")
            if r.strip()
        ]

    # --- clients -------------------------------------------------------------
    async def get_client(self, client_id: str):
        data = _get("oauth-client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not any(str(uri).startswith(p) for p in self.redirects):
                raise RegistrationError("invalid_redirect_uri", "redirect URI not allowed")
        _put("oauth-client", client_info.client_id, json.loads(client_info.model_dump_json()))

    # --- authorize: hand off to Google, owner check happens in the callback ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending_id = secrets.token_urlsafe(32)
        _put(
            "oauth-pending",
            _h(pending_id),
            {
                "client_id": client.client_id,
                "params": json.loads(params.model_dump_json()),
                "exp": time.time() + PENDING_TTL,
            },
        )
        return f"{self.base}/google/login?{urlencode({'p': pending_id})}"

    def complete_authorization(self, pending_id: str) -> str | None:
        """Owner verified: mint a code and return the redirect back to the MCP client."""
        key = _h(pending_id)
        pending = _get("oauth-pending", key)
        tokenstore.delete("oauth-pending", key)  # single use
        if not pending or pending["exp"] < time.time():
            return None
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        _put(
            "oauth-code",
            _h(code),
            AuthorizationCode(
                code=_h(code),
                scopes=params.scopes or [SCOPE],
                expires_at=time.time() + CODE_TTL,
                client_id=pending["client_id"],
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject=os.environ["OWNER_EMAIL"],
            ).model_dump(mode="json"),
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # --- code / token exchange ----------------------------------------------
    async def load_authorization_code(self, client, authorization_code: str):
        data = _get("oauth-code", _h(authorization_code))
        if not data or data["client_id"] != client.client_id or data["expires_at"] < time.time():
            return None
        return AuthorizationCode.model_validate(data)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        tokenstore.delete("oauth-code", authorization_code.code)  # already hashed; single use
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        common = dict(client_id=client_id, scopes=scopes, resource=resource, subject=os.environ["OWNER_EMAIL"])
        _put("oauth-access", _h(access), AccessToken(token=_h(access), expires_at=now + ACCESS_TTL, **common).model_dump(mode="json"))
        _put("oauth-refresh", _h(refresh), RefreshToken(token=_h(refresh), expires_at=now + REFRESH_TTL, **common).model_dump(mode="json"))
        return OAuthToken(
            access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh
        )

    async def load_refresh_token(self, client, refresh_token: str):
        data = _get("oauth-refresh", _h(refresh_token))
        if not data or data["client_id"] != client.client_id or (data["expires_at"] or 0) < time.time():
            return None
        return RefreshToken.model_validate(data)

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        tokenstore.delete("oauth-refresh", refresh_token.token)  # rotate
        granted = scopes or refresh_token.scopes
        if not set(granted) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope")
        return self._issue(client.client_id, granted, refresh_token.resource)

    async def load_access_token(self, token: str):
        data = _get("oauth-access", _h(token))
        if not data or (data["expires_at"] or 0) < time.time():
            return None
        return AccessToken.model_validate(data)

    async def revoke_token(self, token) -> None:
        kind = "oauth-access" if isinstance(token, AccessToken) else "oauth-refresh"
        tokenstore.delete(kind, token.token)  # token.token is already the hash


def build_mcp(name: str) -> FastMCP:
    """FastMCP with owner-only OAuth and stateless JSON streamable HTTP (Lambda friendly)."""
    base = os.environ["MCP_PUBLIC_URL"].rstrip("/")
    provider = OwnerOAuthProvider(base)
    host = urlparse(base).netloc
    mcp = FastMCP(
        name,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host], allowed_origins=[base]
        ),
    )

    @mcp.custom_route("/google/login", methods=["GET"])
    async def google_login(request: Request):
        pending_id = request.query_params.get("p", "")
        if not _get("oauth-pending", _h(pending_id)):
            return PlainTextResponse("Expired or invalid request", status_code=400)
        url = GOOGLE_AUTH + "?" + urlencode(
            {
                "client_id": os.environ["GOOGLE_WEB_CLIENT_ID"],
                "redirect_uri": f"{base}/google/callback",
                "response_type": "code",
                "scope": "openid email",
                "state": pending_id,
                "prompt": "select_account",
            }
        )
        resp = RedirectResponse(url, status_code=302)
        # Binds the callback to this browser so a victim can't be fed someone else's flow.
        resp.set_cookie("mcp_pending", _h(pending_id), max_age=PENDING_TTL, httponly=True, secure=True, samesite="lax")
        return resp

    @mcp.custom_route("/google/callback", methods=["GET"])
    async def google_callback(request: Request):
        pending_id = request.query_params.get("state", "")
        code = request.query_params.get("code")
        cookie = request.cookies.get("mcp_pending", "")
        if not code or not hmac.compare_digest(cookie, _h(pending_id)):
            return PlainTextResponse("Invalid request", status_code=400)
        async with httpx.AsyncClient(timeout=20) as http:
            tok = await http.post(
                GOOGLE_TOKEN,
                data={
                    "code": code,
                    "client_id": os.environ["GOOGLE_WEB_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_WEB_CLIENT_SECRET"],
                    "redirect_uri": f"{base}/google/callback",
                    "grant_type": "authorization_code",
                },
            )
            if tok.status_code != 200:
                return PlainTextResponse("Google sign-in failed", status_code=400)
            info = await http.get(
                GOOGLE_USERINFO, headers={"Authorization": f"Bearer {tok.json()['access_token']}"}
            )
        u = info.json()
        owner = os.environ["OWNER_EMAIL"].lower()
        if not (u.get("email_verified") and str(u.get("email", "")).lower() == owner):
            tokenstore.delete("oauth-pending", _h(pending_id))
            return PlainTextResponse("Forbidden", status_code=403)
        target = provider.complete_authorization(pending_id)
        if not target:
            return PlainTextResponse("Expired request", status_code=400)
        return RedirectResponse(target, status_code=302)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return PlainTextResponse("ok")

    return mcp
