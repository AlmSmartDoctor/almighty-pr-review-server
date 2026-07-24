import hmac
import ipaddress
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from server import config

PUBLIC_API_PATHS = frozenset({
    "/api/health",
    "/api/webhooks/github",
    "/api/webhooks/slack",
})


async def protect_management_api(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Protect management APIs while keeping provider-authenticated webhooks public."""
    path = request.url.path
    normalized_path = path.rstrip("/") or "/"
    if config.EXTERNAL_MODE:
        direct_tls = request.url.scheme == "https"
        forwarded_tls = False
        forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        peer = request.client.host if request.client else ""
        if forwarded == "https" and peer:
            try:
                address = ipaddress.ip_address(peer)
                forwarded_tls = any(
                    address in network for network in config.TRUSTED_PROXY_CIDRS
                )
            except ValueError:
                forwarded_tls = False
        if not direct_tls and not forwarded_tls:
            return JSONResponse(
                status_code=400,
                content={"detail": "external mode requires trusted HTTPS"},
            )
    if config.WEBHOOK_ONLY_INGRESS:
        # The dedicated ingress profile is not an operations/admin listener.  Keep
        # only the provider-authenticated GitHub receiver reachable.
        if normalized_path != "/api/webhooks/github":
            return JSONResponse(status_code=404, content={"detail": "webhook ingress only"})
        return await call_next(request)
    if not path.startswith("/api/") or normalized_path in PUBLIC_API_PATHS:
        return await call_next(request)

    origin = request.headers.get("origin")
    if origin is not None and origin not in config.ADMIN_ALLOWED_ORIGINS:
        return JSONResponse(status_code=403, content={"detail": "origin not allowed"})

    if request.method == "OPTIONS":
        response: Response = JSONResponse(content={})
    else:
        expected = f"Bearer {config.ADMIN_TOKEN}" if config.ADMIN_TOKEN else ""
        supplied = request.headers.get("authorization", "")
        if expected and not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "admin authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        response = await call_next(request)

    if origin in config.ADMIN_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    return response
