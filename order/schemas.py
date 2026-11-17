from pydantic import BaseModel
from typing import Optional



class APIActionResponse(BaseModel):
    status: str
    message: str
    order_id: Optional[str] = None
    order_reference: Optional[str] = None
