# marketplace/routing.py
from django.urls import path
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Captures the unique alphanumeric vendor ID securely from the query line
    re_path(r'^/?ws/vendor/dashboard/(?P<vendor_id>[^/]+)/$', consumers.VendorNotificationConsumer.as_asgi()),
    
]