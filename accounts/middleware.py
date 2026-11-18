import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.http import JsonResponse

from accounts.utils import verify_jwt_token, verify_turnstile_sync

logger = logging.getLogger(__name__)


@database_sync_to_async
def get_user_from_token(token_string):
    # verify_jwt_token checks signature, exp, is_active and token_version
    return verify_jwt_token(token_string)


class TokenAuthMiddleware:
    """
    Channels middleware that extracts the JWT from the query string with parse_qs
    (robust against '=' characters), verifies it (exp, is_active, token_version)
    and injects the user into the scope.

    NOTE: browsers cannot set an Authorization header on WebSocket handshakes, so
    the token travels in the query string. Make sure your reverse proxy does not
    log query strings for /ws/ paths.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode(errors="ignore")
        query_params = parse_qs(query_string)
        token_list = query_params.get("token", [])

        user = None
        if token_list and token_list[0] not in ("", "null", "undefined"):
            user = await get_user_from_token(token_list[0])

        scope["user"] = user if user else AnonymousUser()
        return await self.inner(scope, receive, send)

class JWTUserForWebpushMiddleware:
    """
    django-webpush stores a subscription against `request.user`, which only knows SESSION logins,
    while this API authenticates with JWTs. For /webpush/ requests the Authorization bearer token
    is resolved here so the subscription is saved against the real user. Without it every
    subscription is anonymous and user-targeted pushes (order updates) can never be delivered.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/webpush/"):
            header = request.META.get("HTTP_AUTHORIZATION", "")
            if header.startswith("Bearer "):
                try:
                    user = verify_jwt_token(header.split(" ", 1)[1].strip())
                except Exception:
                    user = None
                if user:
                    request.user = user
        return self.get_response(request)
    
class PlatformSecurityMiddleware:
    """
    Per-IP, per-bucket rate limiting for sensitive endpoints plus a captcha gate
    on registration.

    Settings:
      TRUSTED_PROXY_COUNT  number of reverse proxies in front of Django that
                           append to X-Forwarded-For (default 0 = use REMOTE_ADDR
                           only, which cannot be spoofed by the client).
      TURNSTILE_SECRET_KEY when set, X-Captcha-Token is verified with Cloudflare.

    The counter lives in Django's cache. Use a shared cache (Redis) in production;
    the default per-process LocMemCache lets an attacker multiply their allowance
    by the number of worker processes.
    """

    # (bucket name, path fragments, max requests, window seconds)
    BUCKETS = (
        ("auth", (
            "auth/register/individual", "auth/register/rider", "auth/register/business",
            "auth/login", "auth/login/rider", "auth/customer/login",
            "auth/password/reset/request", "auth/password/reset/confirm",
            "auth/password/change", "auth/password",
        ), 10, 60),
        ("payments", (
            "marketplace/checkout", "marketplace/verify-web", "marketplace/order/verify",
        ), 30, 60),
        ("wallet", (
            "wallet/vendor/payout", "wallet/vendor/set-pin", "wallet/bank-setup",
            "wallet/resolve-account",
        ), 10, 60),
        ("chat", ("support/chat/start",), 6, 60),
        ("public", ("public/newsletter/subscribe", "public/submit-review"), 10, 60),
    )

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _client_ip(request) -> str:
        proxies = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0) or 0)
        if proxies > 0:
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if len(hops) >= proxies:
                return hops[-proxies]
        return request.META.get("REMOTE_ADDR", "unknown")

    def _match_bucket(self, path: str):
        for name, fragments, limit, window in self.BUCKETS:
            if any(fragment in path for fragment in fragments):
                return name, limit, window
        return None

    @staticmethod
    def _hit(key: str, window: int) -> int:
        # cache.add is atomic: it only sets the key if it does not exist yet.
        if cache.add(key, 1, timeout=window):
            return 1
        try:
            return cache.incr(key)
        except ValueError:  # key expired between add() and incr()
            cache.set(key, 1, timeout=window)
            return 1

    def __call__(self, request):
        if request.path.startswith("/api/v1/"):
            bucket = self._match_bucket(request.path)
            if bucket:
                name, limit, window = bucket
                client_ip = self._client_ip(request)
                try:
                    attempts = self._hit(f"platform_security_{name}_{client_ip}", window)
                except Exception:  # cache outage must not take the API down
                    logger.exception("Rate limiter cache failure; failing open")
                    attempts = 0

                if attempts > limit:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Too many requests from this network. Please slow down and try again later.",
                        },
                        status=429,
                    )

                if (
                    request.method in ("POST", "PUT", "PATCH")
                    and "register" in request.path
                    and settings.DEBUG is False
                ):
                    captcha_token = request.headers.get("X-Captcha-Token")
                    if not captcha_token:
                        return JsonResponse(
                            {"status": "error", "message": "Security challenge token missing."},
                            status=400,
                        )
                    if getattr(settings, "TURNSTILE_SECRET_KEY", ""):
                        if not verify_turnstile_sync(captcha_token, self._client_ip(request)):
                            return JsonResponse(
                                {"status": "error", "message": "Security challenge failed."},
                                status=400,
                            )

        return self.get_response(request)
