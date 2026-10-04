"""OAuth routes delegated to the MCP SDK, plus the local-owner pairing page."""

from __future__ import annotations

import html
import re
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import (
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from pydantic import AnyHttpUrl
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Router

from .auth import SCOPES
from .oauth import OAuthProvider, OAuthStore

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def routes_app(state: OAuthStore):
    provider = OAuthProvider(state)
    authenticator = ClientAuthenticator(provider)
    registration_options = ClientRegistrationOptions(
        enabled=True, valid_scopes=sorted(SCOPES), default_scopes=["memory:read"]
    )

    async def register(request):
        try:
            await request.json()
        except (ValueError, UnicodeError):
            return JSONResponse(
                {"error": "invalid_client_metadata"}, status_code=400, headers=NO_STORE
            )
        return await RegistrationHandler(provider, registration_options).handle(request)

    async def token(request):
        form = await request.form()
        # SDK 1.30 parses RFC 8707 resource but does not validate it at /token.
        if form.get("resource") != state.resource:
            return JSONResponse({"error": "invalid_target"}, status_code=400, headers=NO_STORE)
        if form.get("grant_type") == "authorization_code" and not re.fullmatch(
            r"[A-Za-z0-9._~-]{43,128}", str(form.get("code_verifier", ""))
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=NO_STORE)
        return await TokenHandler(provider, authenticator).handle(request)

    async def revoke(request):
        # SDK 1.30's revocation model requires a nullable client_secret field.
        # Normalize the optional public-client field without changing SDK authentication.
        form = dict(await request.form())
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
        return await RevocationHandler(provider, authenticator).handle(normalized)

    async def pairing(request):
        result = await provider.run(state.claim, request.path_params["request_id"])
        if result["state"] == "redirect":
            return RedirectResponse(result["url"], status_code=302, headers=NO_STORE)
        if result["state"] == "expired":
            return HTMLResponse(
                "<h1>This connection request has expired.</h1>", status_code=410, headers=NO_STORE
            )
        request_id = html.escape(result["request_id"])
        page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3"><title>Connect Dots Brain</title>
<body><main><h1>Connect to your Dots Brain</h1>
<p>Waiting for authorization from your memory host.</p>
<p>Your setup agent can authorize this exact request. If it cannot see this page,
give it the request ID below. Never share your memory credentials.</p>
<p><code>{request_id}</code></p><p>This request expires after five minutes.
Once authorized, this page returns you to your AI tool automatically.</p>
</main></body></html>"""
        return HTMLResponse(
            page,
            headers={
                **NO_STORE,
                "Content-Security-Policy": (
                    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                ),
                "X-Frame-Options": "DENY",
                "X-Content-Type-Options": "nosniff",
            },
        )

    routes = create_auth_routes(
        provider,
        AnyHttpUrl(state.issuer),
        client_registration_options=registration_options,
        revocation_options=RevocationOptions(enabled=True),
    )
    routes = [route for route in routes if route.path not in {"/token", "/revoke", "/register"}]
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
    routes.append(Route("/oauth/pair/{request_id}", pairing, methods=["GET"]))
    router = Router(routes=routes)

    async def enabled(scope, receive, send):
        if not state.enabled():
            return await JSONResponse(
                {"error": "temporarily_unavailable"}, status_code=503, headers=NO_STORE
            )(scope, receive, send)
        return await router(scope, receive, send)

    return TrustedHostMiddleware(
        RequestBodyLimitMiddleware(enabled, 16384),
        allowed_hosts=[urlsplit(state.issuer).hostname, "localhost", "127.0.0.1", "[::1]"],
    )
