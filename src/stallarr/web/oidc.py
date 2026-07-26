"""OpenID Connect login, for putting stallarr behind Authentik (or any IdP).

The flow is the standard authorization-code grant with PKCE, run by stallarr
itself rather than by a reverse proxy. That choice is the point of the module:
forward-auth headers are only trustworthy if *every* route to the container goes
through the proxy that sets them, and a homelab container almost always also
answers on a published port and on the docker network. Doing the exchange here
means the app is protected wherever it is reached from.

**Why no JWT verification.** The identity is read from the userinfo endpoint,
not from the ID token, so there is no signature to check and no crypto
dependency to add. The access token was handed to us over a TLS connection to
the issuer's own token endpoint, authenticated with the client secret, and is
then presented back to that same issuer — an attacker who could forge that
response could equally forge a signing key handover. OIDC Core allows exactly
this substitution for the code flow (§3.1.3.7 note 2).

**What the state cookie is for.** The transaction (`state`, PKCE verifier, and
where to return to) is held in a short-lived signed cookie rather than in
process memory, so a restart or a second worker mid-login is a re-login rather
than an error. It is signed, not encrypted: none of it is secret, and the
verifier is only useful to whoever also holds the code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin

import httpx

log = logging.getLogger(__name__)

#: Must match the route in routes.py, and must be registered as a redirect URI
#: in the IdP — they compare it byte for byte.
CALLBACK_PATH = "/api/auth/sso/callback"
TXN_COOKIE = "stallarr_oidc"
#: How long a login may sit half-finished. Long enough to type a password and
#: answer an MFA prompt, short enough that a stale tab does not linger.
TXN_TTL = 600

_DISCOVERY_TTL = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}


class OIDCError(RuntimeError):
    """Anything that should stop a login and be shown to the user."""


@dataclass(slots=True)
class Identity:
    subject: str
    username: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.username or self.email or self.subject


def configured(web_cfg) -> bool:
    """True when SSO is switched on *and* actually usable.

    Enabled-but-incomplete has to read as off. If it counted as on, a
    half-filled form would leave an instance that refuses every login and offers
    no way back in — the config UI is behind the same check.
    """
    return bool(
        web_cfg.oidc_enabled
        and web_cfg.oidc_issuer
        and web_cfg.oidc_client_id
        and web_cfg.oidc_client_secret
    )


# --- discovery ----------------------------------------------------------------


async def discover(web_cfg) -> dict:
    """The issuer's endpoint metadata, cached for an hour.

    Cached because it is fetched on the way *into* a login: an issuer that is
    slow to answer would otherwise add its latency to every sign-in.
    """
    issuer = web_cfg.oidc_issuer.rstrip("/") + "/"
    cached = _discovery_cache.get(issuer)
    if cached and time.time() - cached[0] < _DISCOVERY_TTL:
        return cached[1]

    url = urljoin(issuer, ".well-known/openid-configuration")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise OIDCError(f"could not reach the identity provider: {exc}") from exc
    if resp.status_code >= 400:
        raise OIDCError(f"identity provider discovery returned HTTP {resp.status_code}")
    try:
        meta = resp.json()
    except ValueError as exc:
        raise OIDCError("identity provider discovery did not return JSON") from exc

    for required in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not meta.get(required):
            raise OIDCError(f"identity provider advertises no {required}")

    _discovery_cache[issuer] = (time.time(), meta)
    return meta


def forget_discovery() -> None:
    """Drop the cache. Called on config reload so a corrected issuer takes
    effect immediately instead of an hour later."""
    _discovery_cache.clear()


# --- transaction cookie -------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _seal(secret: str, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{digest}"


def _unseal(secret: str, token: str) -> dict:
    try:
        body, digest = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise OIDCError("login transaction is malformed — start again") from exc
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise OIDCError("login transaction failed its signature check — start again")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise OIDCError("login transaction is unreadable — start again") from exc
    if time.time() > payload.get("exp", 0):
        raise OIDCError("login took too long — start again")
    return payload


# --- the flow -----------------------------------------------------------------


def redirect_uri(web_cfg, request_base: str) -> str:
    """Where the IdP sends the browser back to.

    Order matters. An explicit setting wins, then the configured external base
    URL, and only then the incoming request — because that last one is derived
    from `Host`/`X-Forwarded-Host`, which the client controls. The IdP rejects
    any redirect URI it does not already know, so a spoofed host cannot steal a
    code; it can only produce a confusing failure. Configure one of the first
    two on anything reachable from outside.
    """
    for candidate in (web_cfg.oidc_redirect_url, web_cfg.base_url, request_base):
        base = (candidate or "").strip().rstrip("/")
        if not base:
            continue
        # An explicit redirect URL may already name the callback path.
        return base if base.endswith(CALLBACK_PATH) else base + CALLBACK_PATH
    raise OIDCError("no redirect URL could be determined — set the external base URL")


async def begin(web_cfg, request_base: str, next_path: str = "/") -> tuple[str, str]:
    """Start a login. Returns (URL to send the browser to, transaction cookie)."""
    meta = await discover(web_cfg)

    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(24))
    uri = redirect_uri(web_cfg, request_base)

    params = {
        "response_type": "code",
        "client_id": web_cfg.oidc_client_id,
        "redirect_uri": uri,
        "scope": web_cfg.oidc_scopes or "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    txn = _seal(
        web_cfg.session_secret,
        {"state": state, "verifier": verifier, "uri": uri, "next": next_path,
         "exp": int(time.time()) + TXN_TTL},
    )
    return f"{meta['authorization_endpoint']}?{urlencode(params)}", txn


async def complete(web_cfg, *, code: str, state: str, txn: str) -> tuple[Identity, str]:
    """Finish a login. Returns the identity and where to send the browser."""
    payload = _unseal(web_cfg.session_secret, txn)
    if not hmac.compare_digest(str(payload.get("state", "")), state or ""):
        raise OIDCError("login state did not match — start again")

    meta = await discover(web_cfg)
    tokens = await _exchange(web_cfg, meta, code=code, payload=payload)
    access_token = tokens.get("access_token")
    if not access_token:
        raise OIDCError("identity provider returned no access token")

    identity = await _userinfo(meta, access_token)
    _check_groups(web_cfg, identity)
    return identity, str(payload.get("next") or "/")


async def _exchange(web_cfg, meta: dict, *, code: str, payload: dict) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        # Must be byte-identical to the one sent to the authorization endpoint,
        # so it is replayed from the transaction rather than recomputed.
        "redirect_uri": payload.get("uri", ""),
        "client_id": web_cfg.oidc_client_id,
        "client_secret": web_cfg.oidc_client_secret,
        "code_verifier": payload.get("verifier", ""),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(meta["token_endpoint"], data=form)
    except httpx.HTTPError as exc:
        raise OIDCError(f"could not reach the token endpoint: {exc}") from exc

    if resp.status_code >= 400:
        # The body names the actual fault ("invalid_client", "redirect_uri
        # mismatch"), and every one of those is a configuration error someone
        # has to see to fix. It carries no credential — the secret went the
        # other way.
        raise OIDCError(f"token exchange failed (HTTP {resp.status_code}): {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise OIDCError("token endpoint did not return JSON") from exc


async def _userinfo(meta: dict, access_token: str) -> Identity:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                meta["userinfo_endpoint"], headers={"Authorization": f"Bearer {access_token}"}
            )
    except httpx.HTTPError as exc:
        raise OIDCError(f"could not reach the userinfo endpoint: {exc}") from exc
    if resp.status_code >= 400:
        raise OIDCError(f"userinfo lookup failed (HTTP {resp.status_code})")
    try:
        claims = resp.json()
    except ValueError as exc:
        raise OIDCError("userinfo did not return JSON") from exc

    subject = str(claims.get("sub") or "")
    if not subject:
        raise OIDCError("userinfo returned no subject")
    raw_groups = claims.get("groups") or []
    return Identity(
        subject=subject,
        username=str(claims.get("preferred_username") or claims.get("nickname") or ""),
        email=str(claims.get("email") or ""),
        groups=[str(g) for g in raw_groups] if isinstance(raw_groups, list) else [],
    )


def _check_groups(web_cfg, identity: Identity) -> None:
    """Enforce the group allow-list, failing closed.

    An empty allow-list means "anyone the IdP lets through", which is the right
    default when the IdP is already deciding who may reach the application. Once
    a list exists, a missing `groups` claim is a denial and not a pass: the
    usual cause is the scope not being granted, and treating that as "no groups,
    therefore no restriction" would turn a tightening into an opening.
    """
    allowed = [g.strip() for g in web_cfg.oidc_allowed_groups if g.strip()]
    if not allowed:
        return
    if not identity.groups:
        raise OIDCError(
            "no groups were returned for this account — grant the 'groups' scope "
            "to the application, or clear the allowed-groups list"
        )
    if not set(allowed) & set(identity.groups):
        log.warning("SSO login refused for %s: not in %s", identity.display, allowed)
        raise OIDCError("your account is not in a group allowed to use this application")
