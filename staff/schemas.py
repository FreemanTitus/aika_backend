from typing import Optional, List, Any, Literal
from ninja import Schema
from decimal import Decimal
from pydantic import Field


CategoryLiteral = Literal[
    "gadgets",
    "apparel",
    "appliances",
    "beauty",
    "meals",
    "drinks",
    "sides",
    "general",
]


class ProductUpdateSchema(Schema):
    """
    Validates inventory updates for Restaurants, Pharmacies, and Retailers 
    using optional fields to support partial PUT/PATCH requests.
    """
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    category: Optional[str] = None  # Or CategoryLiteral if defined

    key_features: Optional[str] = None
    
    sub_category_id: Optional[str] = None
    new_sub_category_name: Optional[str] = None

    # Brand Controls
    brand_selection: Optional[str] = None                      
    new_brand_name: Optional[str] = None       
    
    # Promotional Rulesets
    bonanza_mode: Optional[bool] = None
    discount_price: Optional[Decimal] = None
    is_available: Optional[bool] = None

    image_key: Optional[str] = None
    
    # Complex payloads / JSON structures
    variant_options: Optional[Any] = None
    sku_items: Optional[Any] = None
    gallery_images: Optional[Any] = None


class ProductOutSchema(Schema):
    id: str
    name: str
    description: Optional[str] = None
    price: Decimal
    category: str
    bonanza_mode: bool
    discount_price: Optional[Decimal] = None
    is_available: bool


class ReassignOrderSchema(Schema):
    """
    Schema for agents to reassign an active order to a new delivery rider.
    """
    rider_id: str = Field(
        ..., 
        description="The database primary key ID of the target RiderProfile to handle this order."
    )


class CancelOrderSchema(Schema):
    """
    Schema for handling order cancellations and refund flags by support agents.
    """
    reason: Optional[str] = Field(
        default="Cancelled by agent intervention",
        max_length=250,
        description="Operational reason logs explaining why the order was cancelled."
    )
    issue_refund: bool = Field(
        default=True,
        description="Flag indicating whether to automatically disburse refunds back to the customer's wallet or payment gateway."
    )

class RiderInfoSchema(Schema):
    id: str
    name: str
    phone: Optional[str] = None

class VendorOrderSchema(Schema):
    id: str
    customer_name: str
    total_amount: float
    status: str
    assigned_rider: Optional[RiderInfoSchema] = None
    created_at: str

class VendorOrdersResponseSchema(Schema):
    status: str
    orders: List[VendorOrderSchema]
    riders: List[RiderInfoSchema]