import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aika.settings')

# Core HTTP ASGI initialization must execute before importing routing files 
# that might interact with the database model definitions.
django_asgi_app = get_asgi_application()

# Delayed routing imports to prevent AppRegistryNotReady exceptions during boot
from merchant.routing import websocket_urlpatterns
from customers.routing import websocket_urlpatterns as customer_url
from admins.routing import websocket_urlpatterns as admin_url
from rider.routing import websocket_urlpatterns as rider_url
from messaging.routings import websocket_urlpatterns as messaging_url

from accounts.middleware import TokenAuthMiddleware

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": TokenAuthMiddleware(
        AuthMiddlewareStack(
            URLRouter(
                websocket_urlpatterns + customer_url + admin_url + rider_url + messaging_url
            )
        )
    ),
})
