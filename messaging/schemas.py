from ninja import Schema
from typing import Optional


class StartChatSchema(Schema):
    full_name: Optional[str] = None
    email: Optional[str] = None
    initial_message: str

