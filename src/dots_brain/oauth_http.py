"""OAuth routes delegated to the MCP SDK, plus the local-owner pairing page."""

from __future__ import annotations

import html
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import (
    build_metadata,
    cors_middleware,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from pydantic import AnyHttpUrl
from pydantic_core import PydanticSerializationError
from starlette.formparsers import MultiPartException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Router

from .auth import SCOPES
from .errors import InputError
from .oauth import OAuthProvider, OAuthStore

NO_STORE = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}


class OnboardingLimiter:
    """Bound public pairing work and memory used for rate-limit accounting."""

    def __init__(self):
        self.buckets = OrderedDict()
        self.global_buckets = {}

    def allow(self, scope):
        route = scope.get("path")
        if route not in {"/register", "/authorize"}:
            return True
        # The ASGI peer is owned by the listening transport. Forwarded client-IP
        # headers are caller input at this boundary and must never select a bucket.
        peer = (scope.get("client") or ("unknown",))[0]
        now = time.monotonic()
        per_peer = 6 if route == "/register" else 12
        computed = []
        for buckets, key, capacity in (
            (self.global_buckets, route, per_peer * 10),
            (self.buckets, (route, peer), per_peer),
        ):
            tokens, previous = buckets.get(key, (float(capacity), now))
            tokens = min(capacity, tokens + (now - previous) * capacity / 60)
            if tokens < 1:
                return False
            computed.append((buckets, key, tokens))
        # Commit neither bucket until the request fits both budgets. This prevents
        # an already-limited peer from draining the shared route budget.
        for buckets, key, tokens in computed:
            buckets[key] = (tokens - 1, now)
            if buckets is self.buckets:
                buckets.move_to_end(key)
        while len(self.buckets) > 512:
            self.buckets.popitem(last=False)
        return True


def routes_app(state: OAuthStore):
    provider = OAuthProvider(state)
    authenticator = ClientAuthenticator(provider)
    registration_options = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=sorted(SCOPES),
        # Clients such as ChatGPT omit registration scopes, then request both at
        # authorization. Registration grants no access; owner approval still does.
        default_scopes=["memory:read", "memory:write"],
    )

    async def register(request):
        if (await provider.run(state.onboarding_state))["state"] != "open":
            return JSONResponse(
                {"error": "temporarily_unavailable"}, status_code=503, headers=NO_STORE
            )
        try:
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse(
                    {"error": "invalid_client_metadata"}, status_code=415, headers=NO_STORE
                )
            if not isinstance(await request.json(), dict):
                return JSONResponse(
                    {"error": "invalid_client_metadata"}, status_code=400, headers=NO_STORE
                )
        except (ValueError, UnicodeError, TypeError, RecursionError):
            return JSONResponse(
                {"error": "invalid_client_metadata"}, status_code=400, headers=NO_STORE
            )
        try:
            response = await RegistrationHandler(provider, registration_options).handle(request)
        except (InputError, ValueError, TypeError, RecursionError, PydanticSerializationError):
            return JSONResponse(
                {"error": "invalid_client_metadata"}, status_code=400, headers=NO_STORE
            )
        response.headers.update(NO_STORE)
        return response

    async def token(request):
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0]
                != "application/x-www-form-urlencoded"
            ):
                return JSONResponse({"error": "invalid_request"}, status_code=415, headers=NO_STORE)
            form = await request.form()
        except (ValueError, UnicodeError, TypeError, MultiPartException):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=NO_STORE)
        # SDK 1.30 parses RFC 8707 resource but does not validate it at /token.
        if form.get("resource") != state.resource:
            return JSONResponse({"error": "invalid_target"}, status_code=400, headers=NO_STORE)
        if form.get("grant_type") == "authorization_code" and not re.fullmatch(
            r"[A-Za-z0-9._~-]{43,128}", str(form.get("code_verifier", ""))
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=NO_STORE)
        response = await TokenHandler(provider, authenticator).handle(request)
        response.headers.update(NO_STORE)
        return response

    async def revoke(request):
        # SDK 1.30's revocation model requires a nullable client_secret field.
        # Normalize the optional public-client field without changing SDK authentication.
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0]
                != "application/x-www-form-urlencoded"
            ):
                return JSONResponse({"error": "invalid_request"}, status_code=415, headers=NO_STORE)
            form = dict(await request.form())
        except (ValueError, UnicodeError, TypeError, MultiPartException):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=NO_STORE)
        form.setdefault("client_secret", "")
        body = urlencode(form).encode()
        headers = [
            (k, v)
            for k, v in request.scope["headers"]
            if k.lower() not in {b"content-length", b"content-type"}
        ]
        headers += [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ]

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        normalized = Request({**request.scope, "headers": headers}, receive)
        response = await RevocationHandler(provider, authenticator).handle(normalized)
        response.headers.update(NO_STORE)
        return response

    async def authorize(request):
        response = await AuthorizationHandler(provider).handle(request)
        location = response.headers.get("location", "")
        prefix = state.issuer + "/oauth/pair/"
        if location.startswith(prefix):
            request_id = location[len(prefix) :]
            if re.fullmatch(r"req_[A-Za-z0-9_-]{16,128}", request_id):
                binding = await provider.run(state.pairing, request_id, bind_browser=True)
                nonce = binding.get("browser_nonce")
                if nonce:
                    response.set_cookie(
                        "dots_brain_pair_" + request_id[-32:],
                        nonce,
                        httponly=True,
                        samesite="lax",
                        secure=urlsplit(state.issuer).scheme == "https",
                        path=f"/oauth/pair/{request_id}",
                    )
        return response

    async def pairing(request):
        request_id = request.path_params["request_id"]
        cookie_name = "dots_brain_pair_" + request_id[-32:]
        provided_nonce = request.cookies.get(cookie_name)
        try:
            if request.method == "POST":
                result = await provider.run(
                    state.claim, request_id, provided_nonce, require_browser=True
                )
                if result["state"] == "redirect":
                    response = RedirectResponse(result["url"], status_code=303, headers=NO_STORE)
                    response.delete_cookie(cookie_name, path=f"/oauth/pair/{request_id}")
                    return response
                if result["state"] == "browser_mismatch":
                    return HTMLResponse(
                        "<h1>Open this connection request in the browser that started it.</h1>",
                        status_code=403,
                        headers=NO_STORE,
                    )
                if result["state"] == "pending":
                    result = await provider.run(
                        state.pairing, request_id, browser_nonce=provided_nonce
                    )
            else:
                result = await provider.run(
                    state.pairing,
                    request_id,
                    bind_browser=False,
                    browser_nonce=provided_nonce,
                )
        except InputError:
            return HTMLResponse(
                "<h1>OAuth onboarding is unavailable.</h1>", status_code=503, headers=NO_STORE
            )
        if result["state"] == "redirect":
            # GET and HEAD intentionally never consume an approved grant.
            return RedirectResponse(result["url"], status_code=303, headers=NO_STORE)
        if result["state"] == "expired":
            return HTMLResponse(
                "<h1>This connection request has expired.</h1>", status_code=410, headers=NO_STORE
            )
        request_id_html = html.escape(result["request_id"])
        client_name = html.escape(result["untrusted_client_name"])
        origin = html.escape(result["redirect_origin"])
        requested = "".join(
            f"<li>{html.escape(scope)}</li>" for scope in result["requested_scopes"]
        )
        approval = (
            "Waiting for authorization from your memory host."
            if result["state"] == "pending"
            else "Authorization was denied. Return to your AI tool."
            if result["state"] == "denied"
            else "Authorization is ready. Confirm below to return to your AI tool."
        )
        submit = (
            ""
            if result["state"] not in {"approved", "denied"} or not result["browser_bound"]
            else '<form method="post"><button type="submit">Continue</button></form>'
        )
        browser_note = (
            ""
            if result["browser_bound"]
            else "<p>Refresh this page in the browser that began this connection.</p>"
        )
        refresh_note = (
            "<p>This page checks again in ten seconds. "
            "You can also refresh it after the owner approves.</p>"
            if result["state"] == "pending"
            else ""
        )
        page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="10">' if result["state"] == "pending" else ""}
<title>Connect Dots Brain</title>
<body><main><h1>Connect to your Dots Brain</h1>
<p>{approval}</p>
<p><strong>Untrusted client name:</strong> {client_name}</p>
<p><strong>Redirect origin:</strong> {origin}</p>
<p><strong>Requested scopes:</strong></p><ul>{requested}</ul>
<p>Request ID: <code>{request_id_html}</code></p>
<p>This request expires after five minutes. Never share memory credentials.</p>
{refresh_note}
{browser_note}{submit}
</main></body></html>"""
        response = HTMLResponse(
            page,
            headers={
                **NO_STORE,
                "Content-Security-Policy": (
                    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                    f"form-action 'self' {origin}"
                ),
                "X-Frame-Options": "DENY",
                "X-Content-Type-Options": "nosniff",
            },
        )
        if result["browser_nonce"] is not None:
            response.set_cookie(
                cookie_name,
                result["browser_nonce"],
                httponly=True,
                samesite="lax",
                secure=urlsplit(state.issuer).scheme == "https",
                path=f"/oauth/pair/{request_id}",
            )
        return response

    routes = [Route("/authorize", authorize, methods=["GET", "POST"])]
    metadata = build_metadata(
        AnyHttpUrl(state.issuer), None, registration_options, RevocationOptions(enabled=True)
    )
    # SDK 1.30 accepts public clients but omits their auth method from discovery.
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post"]
    metadata.revocation_endpoint_auth_methods_supported = ["none", "client_secret_post"]
    routes.append(
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    )
    for path, endpoint in (("/token", token), ("/revoke", revoke), ("/register", register)):
        routes.append(
            Route(
                path,
                endpoint=cors_middleware(endpoint, ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            )
        )
    routes.extend(
        create_protected_resource_routes(
            AnyHttpUrl(state.resource),
            [AnyHttpUrl(state.issuer)],
            scopes_supported=sorted(SCOPES),
            resource_name="Dots Brain",
        )
    )
    routes.append(Route("/oauth/pair/{request_id}", pairing, methods=["GET", "HEAD", "POST"]))
    router = Router(routes=routes)
    limiter = OnboardingLimiter()

    async def enabled(scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await router(scope, receive, send)
        if not limiter.allow(scope):
            return await JSONResponse(
                {"error": "rate_limited"},
                status_code=429,
                headers={**NO_STORE, "Retry-After": "10"},
            )(scope, receive, send)
        if not await provider.run(state.enabled):
            return await JSONResponse(
                {"error": "temporarily_unavailable"}, status_code=503, headers=NO_STORE
            )(scope, receive, send)
        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            return await router(scope, receive, guarded_send)
        except (
            ValueError,
            TypeError,
            RecursionError,
            PydanticSerializationError,
            MultiPartException,
        ):
            if not started:
                return await JSONResponse(
                    {"error": "invalid_request"}, status_code=400, headers=NO_STORE
                )(scope, receive, send)
            raise
        except Exception as exc:
            logging.getLogger("dots_brain").warning(
                "OAuth request failed exception_type=%s", type(exc).__name__
            )
            if not started:
                return await JSONResponse(
                    {"error": "server_error"}, status_code=503, headers=NO_STORE
                )(scope, receive, send)
            raise

    protected = TrustedHostMiddleware(
        RequestBodyLimitMiddleware(enabled, 16384),
        allowed_hosts=[urlsplit(state.issuer).hostname, "localhost", "127.0.0.1", "[::1]"],
    )

    async def with_security_headers(scope, receive, send):
        async def response_send(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                # This is the single response-header owner. Individual handlers
                # may supply a stricter route-specific CSP, which must not be
                # overwritten here.
                for key, value in NO_STORE.items():
                    headers.setdefault(key.lower().encode(), value.encode())
                message = {**message, "headers": list(headers.items())}
            await send(message)

        return await protected(scope, receive, response_send)

    return with_security_headers
