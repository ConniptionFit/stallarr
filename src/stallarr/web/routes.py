"""HTTP API for the dashboard.

Two ways in, both landing on the same HMAC-signed session cookie: a shared
password, and OIDC single sign-on (see `web/oidc.py`). Either, both, or neither
— with neither, the API is open, which is a reasonable default for a tool that
starts life on a home LAN and a terrible one anywhere else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from .. import config as config_module
from .. import db
from ..config import Config
from ..logging_setup import recent_logs
from ..models import DEFAULT_LADDER, ActionKind, Ailment
from ..service import StallarrService
from ..trackers import registry
from . import oidc

log = logging.getLogger(__name__)

COOKIE_NAME = "stallarr_session"
SESSION_TTL = 30 * 86400
#: Subject recorded for a password login, which has no identity behind it.
LOCAL_SUBJECT = "local"


def _service(request: Request) -> StallarrService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service is still starting")
    return service


def _session_key(web_cfg) -> str:
    """Signing key for session cookies, bound to the current credentials.

    Mixing the password in means changing it revokes every outstanding session.
    Signing with the session secret alone would leave old cookies valid, so
    rotating the password after a leak would lock nobody out — the one moment
    you most need it to. The OIDC client secret is folded in for the same
    reason: revoking the application in the IdP should end the sessions it
    issued, and without this it would not.
    """
    material = f"{web_cfg.session_secret}:{web_cfg.password}:{web_cfg.oidc_client_secret}".encode()
    return hashlib.sha256(material).hexdigest()


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return (forwarded or request.url.scheme) == "https"


def _request_base(request: Request) -> str:
    """The external origin this request arrived on, honouring proxy headers."""
    scheme = "https" if _is_https(request) else "http"
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    return f"{scheme}://{host or request.url.netloc}"


def _sign(secret: str, expires: int, subject: str = LOCAL_SUBJECT) -> str:
    """Session token: `expires.subject.digest`, signed over the first two.

    The subject is inside the signature rather than alongside it, so a session
    cannot be re-pointed at another identity by editing the cookie.
    """
    encoded = base64.urlsafe_b64encode(subject.encode()).decode().rstrip("=")
    body = f"{expires}.{encoded}"
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{digest}"


def _session_subject(secret: str, token: str) -> str | None:
    """The authenticated subject, or None if the token is invalid or expired."""
    try:
        raw_expires, encoded, _digest = token.split(".", 2)
        expires = int(raw_expires)
        padded = encoded + "=" * (-len(encoded) % 4)
        subject = base64.urlsafe_b64decode(padded).decode()
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    if expires < time.time():
        return None
    if not hmac.compare_digest(_sign(secret, expires, subject), token):
        return None
    return subject


def auth_required(web_cfg) -> bool:
    """Whether anything guards this instance at all."""
    return bool(web_cfg.password) or oidc.configured(web_cfg)


def _authenticated(request: Request, web_cfg) -> str | None:
    return _session_subject(_session_key(web_cfg), request.cookies.get(COOKIE_NAME, ""))


def require_auth(request: Request) -> None:
    web_cfg = _service(request).cfg.web
    if not auth_required(web_cfg):
        return
    if _authenticated(request, web_cfg) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


def _issue_session(response: Response, request: Request, web_cfg, subject: str) -> None:
    expires = int(time.time()) + SESSION_TTL
    response.set_cookie(
        COOKIE_NAME,
        _sign(_session_key(web_cfg), expires, subject),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        # Only when the request actually arrived over TLS: setting Secure on a
        # plain-HTTP LAN instance would make the cookie unusable. Honours the
        # proxy header, since TLS usually terminates upstream.
        secure=_is_https(request),
    )


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")
    guarded = [Depends(require_auth)]

    # --- auth -----------------------------------------------------------------

    @router.get("/auth/status")
    async def auth_status(request: Request) -> dict:
        web_cfg = _service(request).cfg.web
        required = auth_required(web_cfg)
        subject = _authenticated(request, web_cfg)
        return {
            "auth_required": required,
            "authenticated": not required or subject is not None,
            "user": subject,
            "password_login": bool(web_cfg.password),
            "sso": {
                "enabled": oidc.configured(web_cfg),
                "name": web_cfg.oidc_display_name or "Single sign-on",
                # Only ever auto-redirect when SSO is the *only* way in.
                # Bouncing past a password form that still works would strand
                # anyone whose IdP is the thing that broke.
                "auto_redirect": bool(
                    web_cfg.oidc_auto_redirect
                    and oidc.configured(web_cfg)
                    and not web_cfg.password
                ),
            },
        }

    @router.post("/auth/login")
    async def login(request: Request, response: Response, payload: dict = Body(...)) -> dict:
        service = _service(request)
        expected = service.cfg.web.password
        if not expected:
            return {"ok": True, "detail": "password login is disabled"}

        supplied = str(payload.get("password", ""))
        if not hmac.compare_digest(supplied, expected):
            # Slow brute force down without holding a worker for long.
            import asyncio

            await asyncio.sleep(1.0)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect password")

        _issue_session(response, request, service.cfg.web, LOCAL_SUBJECT)
        return {"ok": True}

    @router.post("/auth/logout")
    async def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    # --- single sign-on -------------------------------------------------------
    # Unguarded by necessity: these are how you get a session in the first
    # place. Both are GETs because a browser redirect is a GET.

    @router.get("/auth/sso/start", include_in_schema=False)
    async def sso_start(request: Request, next: str = "/") -> Response:
        web_cfg = _service(request).cfg.web
        if not oidc.configured(web_cfg):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "single sign-on is not configured")
        # Only same-site paths: an attacker-supplied `next` is an open redirect,
        # and this one would carry a freshly minted session with it.
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        try:
            url, txn = await oidc.begin(web_cfg, _request_base(request), target)
        except oidc.OIDCError as exc:
            return _sso_failure(str(exc))

        response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            oidc.TXN_COOKIE,
            txn,
            max_age=oidc.TXN_TTL,
            httponly=True,
            samesite="lax",  # must survive the IdP's redirect back
            secure=_is_https(request),
        )
        return response

    @router.get("/auth/sso/callback", include_in_schema=False)
    async def sso_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = "",
        error_description: str = "",
    ) -> Response:
        web_cfg = _service(request).cfg.web
        if error:
            return _sso_failure(error_description or error)
        if not code:
            return _sso_failure("the identity provider returned no authorization code")

        try:
            identity, target = await oidc.complete(
                web_cfg, code=code, state=state, txn=request.cookies.get(oidc.TXN_COOKIE, "")
            )
        except oidc.OIDCError as exc:
            log.warning("SSO login failed: %s", exc)
            return _sso_failure(str(exc))

        log.info("SSO login for %s", identity.display)
        response = RedirectResponse(target or "/", status_code=status.HTTP_303_SEE_OTHER)
        _issue_session(response, request, web_cfg, identity.display or identity.subject)
        response.delete_cookie(oidc.TXN_COOKIE)
        return response

    # --- state ----------------------------------------------------------------

    @router.get("/status", dependencies=guarded)
    async def get_status(request: Request) -> dict:
        return await _service(request).dashboard()

    @router.get("/torrents", dependencies=guarded)
    async def get_torrents(request: Request, only_issues: bool = False) -> dict:
        """Full torrent list with per-torrent diagnosis, for the main table."""
        from ..diagnosis import diagnose_local, should_skip
        from ..service import _diagnosis_json

        service = _service(request)
        if service.client is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "torrent client not connected")

        from ..clients.base import ClientError

        try:
            torrents = await service.client.list_torrents()
        except ClientError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

        ignored = await db.ignored_hashes()
        rows = []
        for torrent in torrents:
            skip_reason = should_skip(torrent, service.cfg.rules, ignored)
            plugin = service.router.for_hosts(torrent.tracker_hosts)
            diagnosis = diagnose_local(torrent, service.cfg.rules, plugin)
            if only_issues and diagnosis.healthy:
                continue
            row = _diagnosis_json(diagnosis)
            row["skipped"] = skip_reason
            row["healthy"] = diagnosis.healthy
            rows.append(row)
        return {"torrents": rows, "count": len(rows)}

    @router.get("/history", dependencies=guarded)
    async def get_history(
        limit: int = 200,
        infohash: str | None = None,
        action: str | None = None,
        ok: bool | None = None,
    ) -> dict:
        return {
            "history": await db.get_history(
                limit=limit, infohash=infohash, action=action, ok=ok
            )
        }

    @router.get("/logs", dependencies=guarded)
    async def get_logs(limit: int = 200, level: str | None = None) -> dict:
        return {"logs": recent_logs(limit=limit, level=level)}

    @router.get("/tracker-stats", dependencies=guarded)
    async def tracker_stats(request: Request) -> dict:
        return await _service(request).tracker_stats()

    # --- actions --------------------------------------------------------------

    @router.post("/scan", dependencies=guarded)
    async def trigger_scan(request: Request) -> dict:
        service = _service(request)
        if service.scanning:
            return {"ok": True, "detail": "a scan is already running"}
        service.trigger_scan()
        return {"ok": True, "detail": "scan queued"}

    @router.post("/torrents/{infohash}/repair", dependencies=guarded)
    async def repair(request: Request, infohash: str) -> dict:
        return await _service(request).repair_now(infohash)

    @router.post("/torrents/{infohash}/reset", dependencies=guarded)
    async def reset(request: Request, infohash: str) -> dict:
        await _service(request).reset_ladder(infohash)
        return {"ok": True, "detail": "recovery ladder reset"}

    @router.post("/torrents/{infohash}/ignore", dependencies=guarded)
    async def ignore(infohash: str, payload: dict = Body(default={})) -> dict:
        await db.add_ignore(infohash, payload.get("name", ""), payload.get("reason", ""))
        return {"ok": True}

    @router.delete("/torrents/{infohash}/ignore", dependencies=guarded)
    async def unignore(infohash: str) -> dict:
        await db.remove_ignore(infohash)
        return {"ok": True}

    @router.get("/ignores", dependencies=guarded)
    async def list_ignores() -> dict:
        return {"ignores": await db.list_ignores()}

    @router.post("/notify/test", dependencies=guarded)
    async def notify_test(request: Request) -> dict:
        return await _service(request).notifier.test()

    # --- config ---------------------------------------------------------------

    @router.get("/config", dependencies=guarded)
    async def get_config(request: Request) -> dict:
        cfg = _service(request).cfg
        return _redact_config(cfg)

    @router.put("/config", dependencies=guarded)
    async def put_config(request: Request, payload: dict = Body(...)) -> dict:
        service = _service(request)
        merged = _merge(service.cfg.model_dump(mode="json"), payload)
        try:
            new_cfg = Config.model_validate(merged)
        except Exception as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid config: {exc}") from exc

        config_module.save(new_cfg)
        await service.reload()
        # An env-pinned field is reapplied by load(), so a save that touched one
        # silently did nothing. Say so instead of reporting a clean success.
        ignored = _pinned_in_payload(payload)
        return {
            "ok": True,
            "config": _redact_config(service.cfg),
            "ignored": ignored,
        }

    @router.get("/plugins", dependencies=guarded)
    async def plugins(request: Request) -> dict:
        """Every registered tracker plugin, configured or not.

        The UI builds its tracker settings form from this, so a newly added
        plugin file shows up with no frontend changes.
        """
        from ..config import TrackerConfig
        from ..trackers import build_plugin, configured_tracker_ids

        service = _service(request)
        out = []
        for plugin_id in configured_tracker_ids(service.cfg):
            configured = service.cfg.trackers.get(plugin_id)
            # Capabilities can depend on which credentials are set (IPTorrents
            # gains lookups only with a cookie), so always read them off an
            # instance built from the current config — never off the class.
            live = service.router.get(plugin_id)
            if live is None:
                try:
                    live = build_plugin(plugin_id, configured or TrackerConfig())
                except Exception:  # noqa: BLE001
                    live = None
            plugin_cls = registry.get(plugin_id)
            out.append(
                {
                    "id": plugin_id,
                    "name": (live.name if live else "") or plugin_id,
                    "default_url": plugin_cls.default_url if plugin_cls else "",
                    "hosts": list(live.all_hosts) if live else [],
                    "capabilities": sorted(str(c) for c in live.capabilities) if live else [],
                    # Drives which credential boxes the settings form renders.
                    "credential_fields": list(live.credential_fields) if live else [],
                    "enabled": bool(configured and configured.enabled),
                    "status": service.tracker_status.get(plugin_id),
                    # Config-defined trackers have no file in trackers/; the UI
                    # labels them so it is obvious where they came from.
                    "builtin": plugin_cls is not None,
                    "source": configured.source if configured else "",
                }
            )
        return {"plugins": out}

    @router.get("/meta", dependencies=guarded)
    async def meta() -> dict:
        """Static vocabulary the UI renders (ailments, ladder steps, env pins)."""
        return {
            "ailments": [str(a) for a in Ailment],
            "actions": [str(a) for a in DEFAULT_LADDER],
            # Every action that can appear in history, which is *not* the same
            # list: REDOWNLOAD is a real recorded action but not a ladder step,
            # so a history filter built from `actions` could never find one.
            "action_kinds": [str(a) for a in ActionKind],
            # Env vars are fixed for the process lifetime, so this belongs with
            # the other static vocabulary rather than being refetched per save.
            "env_pinned": config_module.env_pinned_paths(),
        }

    # --- prowlarr -------------------------------------------------------------

    @router.post("/prowlarr/test", dependencies=guarded)
    async def prowlarr_test(request: Request, payload: dict = Body(default={})) -> dict:
        """Check the URL and key work before anyone commits them to config."""
        from ..integrations import prowlarr as prowlarr_module

        service = _service(request)
        cfg = _prowlarr_cfg(service.cfg, payload)
        try:
            indexers = await prowlarr_module.ProwlarrClient(cfg).indexers()
        except prowlarr_module.ProwlarrError as exc:
            return {"ok": False, "detail": str(exc)}
        torrent = [i for i in indexers if i.get("protocol") in (None, "torrent")]
        return {"ok": True, "detail": f"{len(torrent)} torrent indexer(s) found"}

    @router.post("/prowlarr/preview", dependencies=guarded)
    async def prowlarr_preview(request: Request, payload: dict = Body(default={})) -> dict:
        """What an import would do, without changing anything."""
        from ..integrations import prowlarr as prowlarr_module

        service = _service(request)
        cfg = _prowlarr_cfg(service.cfg, payload)
        try:
            indexers = await prowlarr_module.ProwlarrClient(cfg).indexers()
            proposals = await prowlarr_module.plan(indexers)
        except prowlarr_module.ProwlarrError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        # `public()` deliberately reports only which secrets exist, not their
        # values — this response reaches the browser.
        return {"proposals": [p.public() for p in proposals]}

    @router.post("/prowlarr/import", dependencies=guarded)
    async def prowlarr_import(request: Request, payload: dict = Body(default={})) -> dict:
        from ..integrations import prowlarr as prowlarr_module

        service = _service(request)
        cfg = service.cfg.model_copy(deep=True)
        cfg.prowlarr = _prowlarr_cfg(service.cfg, payload)

        try:
            result = await prowlarr_module.sync(
                cfg,
                overwrite=bool(payload.get("overwrite")),
                enable=payload.get("enable", True),
            )
        except prowlarr_module.ProwlarrError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

        config_module.save(cfg)
        await service.reload()
        result["ok"] = True
        result["config"] = _redact_config(service.cfg)
        return result

    return router


def _sso_failure(detail: str) -> Response:
    """Send a failed login back to the sign-in page with the reason attached.

    Rendering the error here instead would leave the user on a dead-end page
    with no way to retry, and every one of these failures is a configuration
    problem whose text is the whole diagnosis.
    """
    from urllib.parse import quote

    return RedirectResponse(f"/?sso_error={quote(detail[:300])}", status_code=status.HTTP_303_SEE_OTHER)


def _prowlarr_cfg(cfg: Config, payload: dict) -> config_module.ProwlarrConfig:
    """Prowlarr settings from the request, falling back to what is saved.

    Lets the UI test credentials that have not been saved yet, while still
    honouring the redaction sentinel for a key the browser never received.
    """
    url = str(payload.get("url") or "").strip() or cfg.prowlarr.url
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key or api_key == "__SET__":
        api_key = cfg.prowlarr.api_key
    return config_module.ProwlarrConfig(
        enabled=True, url=url, api_key=api_key, auto_sync=cfg.prowlarr.auto_sync
    )


#: Substrings that mark a config value as a secret. Matched anywhere in the key,
#: so "token" also covers telegram_bot_token and ntfy_token. Any new secret field
#: MUST match one of these or `_redact_config` will hand it to the browser.
_SECRET_FIELDS = (
    "password",
    "api_key",
    "passkey",
    "cookie",
    # Covers session_secret and oidc_client_secret alike. Deliberately the bare
    # word: the specific-name version of this list is what let ntfy_token
    # through, and a client secret reaching the browser is worse.
    "secret",
    "token",
    "webhook_url",
)


def _redact_config(cfg: Config) -> dict[str, Any]:
    """Replace secrets with a sentinel so the UI can show 'set' without leaking."""
    data = json.loads(cfg.model_dump_json())
    return _walk_redact(data)


def _walk_redact(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if any(marker in key for marker in _SECRET_FIELDS) and isinstance(value, str) and value:
                out[key] = "__SET__"
            else:
                out[key] = _walk_redact(value)
        return out
    if isinstance(node, list):
        return [_walk_redact(item) for item in node]
    return node


def _pinned_in_payload(payload: dict) -> list[dict[str, str]]:
    """Paths in this update that an env var will override on the next load."""
    pinned = config_module.env_pinned_paths()
    if not pinned:
        return []

    present: list[dict[str, str]] = []

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = f"{prefix}{key}"
            if path in pinned:
                present.append({"path": path, "env": pinned[path]})
            walk(value, f"{path}.")

    walk(payload, "")
    return present


def _merge(base: dict, incoming: dict) -> dict:
    """Deep-merge an update, treating the redaction sentinel as 'keep existing'."""
    out = dict(base)
    for key, value in incoming.items():
        if value == "__SET__":
            continue  # unchanged secret came back from the UI
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out
