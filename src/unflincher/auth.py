"""Verifies the Cloudflare Access signed JWT in-app. See technical design §6.5: dash/status
accept 'Access + localhost-binding' only because they can't be modified; diary is custom
code, so it also checks the JWT signature/claims to stop any other local process on the
shared VM from reaching this app's port unauthenticated."""
import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from unflincher.config import Settings

CLOCK_SKEW_LEEWAY_SECONDS = 60


class AccessJWTMiddleware(BaseHTTPMiddleware):
    EXEMPT_PATHS = {"/healthz", "/robots.txt"}

    def __init__(self, app, settings: Settings, jwks_client=None):
        super().__init__(app)
        self.settings = settings
        self.jwks_client = jwks_client or (
            jwt.PyJWKClient(f"https://{settings.cf_team_domain}.cloudflareaccess.com/cdn-cgi/access/certs")
            if settings.cf_team_domain else None
        )

    async def dispatch(self, request: Request, call_next):
        if not self.settings.require_access_auth or request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            return JSONResponse({"detail": "missing Cloudflare Access token"}, status_code=403)

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.settings.cf_access_aud,
                issuer=f"https://{self.settings.cf_team_domain}.cloudflareaccess.com",
                options={"require": ["exp", "iat", "aud", "iss"]},
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            )
        except jwt.PyJWTError as exc:
            return JSONResponse({"detail": f"invalid Cloudflare Access token: {exc}"}, status_code=403)

        if claims.get("email") != self.settings.operator_email:
            return JSONResponse({"detail": "token email does not match operator"}, status_code=403)

        return await call_next(request)
