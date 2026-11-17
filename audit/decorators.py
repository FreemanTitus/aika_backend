import asyncio
import logging
from functools import wraps

from asgiref.sync import sync_to_async

from .models import SystemAuditLog

logger = logging.getLogger(__name__)


def _client_ip(request):
    # Imported lazily to avoid a circular import at start-up. Uses the same
    # TRUSTED_PROXY_COUNT logic as the rate limiter, so a client can no longer forge
    # the IP written to the audit trail through an X-Forwarded-For header.
    from accounts.middleware import PlatformSecurityMiddleware
    ip = PlatformSecurityMiddleware._client_ip(request)
    return ip if ip and ip != 'unknown' else None


def _was_denied(response) -> bool:
    """Endpoints often *return* (403, {...}) instead of raising; never audit those as successes."""
    return isinstance(response, tuple) and len(response) >= 1 and isinstance(response[0], int) and response[0] >= 400


def _resolve(action_name, target_getter, response, request, args, kwargs):
    resolved_action = action_name(response, request, *args, **kwargs) if callable(action_name) else action_name
    if callable(target_getter):
        target_entity = target_getter(request, *args, **kwargs)
    else:
        target_entity = str(target_getter or "Platform Entity")
    return resolved_action, target_entity


def log_system_action(action_name, target_getter=None):
    """
    Decorator supporting both sync and async Django Ninja endpoints.
    action_name can be a string or a callable taking (response, request, *args, **kwargs).
    Denied/failed responses are not recorded as executed actions.
    """
    def decorator(view_func):
        if asyncio.iscoroutinefunction(view_func):
            @wraps(view_func)
            async def async_wrapper(request, *args, **kwargs):
                response = await view_func(request, *args, **kwargs)

                try:
                    user = getattr(request, 'user', None)
                    if user and user.is_authenticated and not _was_denied(response):
                        resolved_action, target_entity = _resolve(
                            action_name, target_getter, response, request, args, kwargs
                        )
                        await sync_to_async(SystemAuditLog.objects.create)(
                            user=user,
                            action=resolved_action,
                            target_entity=target_entity,
                            details=f"Action '{resolved_action}' executed successfully.",
                            ip_address=_client_ip(request),
                        )
                except Exception:
                    logger.exception("Async audit log auto-save error")

                return response
            return async_wrapper

        @wraps(view_func)
        def sync_wrapper(request, *args, **kwargs):
            response = view_func(request, *args, **kwargs)
            try:
                user = getattr(request, 'user', None)
                if user and user.is_authenticated and not _was_denied(response):
                    resolved_action, target_entity = _resolve(
                        action_name, target_getter, response, request, args, kwargs
                    )
                    SystemAuditLog.objects.create(
                        user=user,
                        action=resolved_action,
                        target_entity=target_entity,
                        details=f"Action '{resolved_action}' executed successfully.",
                        ip_address=_client_ip(request),
                    )
            except Exception:
                logger.exception("Sync audit log auto-save error")
            return response
        return sync_wrapper
    return decorator
