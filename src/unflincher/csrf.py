"""Double-submit-cookie CSRF check + an Origin/Referer-vs-Host second layer (technical
design §6.5). No server-side session is needed: the cookie IS the secret; an attacker's
cross-site page can trigger a request but can't read the victim's cookie to also send it
back as a header, so the two won't match."""
import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

COOKIE_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_of(url: str) -> str:
    return urlparse(url).netloc


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cookie_token = request.cookies.get(COOKIE_NAME)

        if request.method not in SAFE_METHODS:
            header_token = request.headers.get(HEADER_NAME)
            if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
                return JSONResponse({"detail": "missing or invalid CSRF token"}, status_code=403)

            host = request.headers.get("host", "")
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or referer
            if source and _host_of(source) != host:
                return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)

        response = await call_next(request)

        if not cookie_token:
            response.set_cookie(COOKIE_NAME, secrets.token_urlsafe(32), httponly=False, samesite="strict")

        return response
