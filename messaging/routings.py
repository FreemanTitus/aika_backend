from django.urls import re_path
from .consumers import SupportChatConsumer

websocket_urlpatterns = [
    re_path(r'ws/support/queue/$', SupportChatConsumer.as_asgi()),
    re_path(r'ws/support/(?P<conv_id>[\w-]+)/$', SupportChatConsumer.as_asgi()),
]
