# marketplace/routing.py
from django.urls import path
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/rider/notifications/?$", consumers.RiderDashboardConsumer.as_asgi()),
]