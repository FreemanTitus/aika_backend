
from ninja import Schema
from typing import Optional



class CartQuantityUpdateInput(Schema):
    action: str  # expects 'increment' or 'decrement' (or an optional explicit 'quantity')
    quantity: Optional[int] = None

    
class AddCartItemInput(Schema):
    product_id: str
    quantity: int = 1
    store_id: Optional[str] = None
    name: Optional[str] = None
    price: Optional[float] = 0.00
    vendor_name: Optional[str] = None
    menu_option_id: Optional[int] = None

