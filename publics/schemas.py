from ninja import Schema
from typing import Literal

class NewsletterSubscribeSchema(Schema):
    email: str

class NewsletterBroadcastSchema(Schema):
    subject: str
    content: str
    template_type: str = "standard"

class BroadcastCreateSchema(Schema):
    title: str
    message: str
    target_audience: Literal['all', 'merchants', 'riders', 'customers']