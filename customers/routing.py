# marketplace/routing.py
from django.urls import path
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^/?ws/customer/tracking/(?P<user_id>[^/]+)/$', consumers.CustomerTrackingConsumer.as_asgi()),
    re_path(r'^/?ws/customer/notifications/$', consumers.NotificationConsumer.as_asgi()),
    
]