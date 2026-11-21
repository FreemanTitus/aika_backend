# marketplace/schemas.py
from typing import Literal, Optional, List, Any
from ninja import Schema
from decimal import Decimal
from pydantic import Field

# =========================================================================
# QUERY FILTER UTILITIES (FOR CLIENT GET REQUESTS)
# =========================================================================

CategoryLiteral = Literal[
    "gadgets",
    "apparel",
    "appliances",
    "beauty",
    "restaurant",
    "general",
]

class QuoteRequestInput(Schema):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    items: List[dict] = []
    
class QuoteRequestSchema(Schema):
    merchant_id: str
    address_id: Optional[int] = None
    street_address: Optional[str] = None
    # base_location: Optional[int, str] = None

class CategoryFilterSchema(Schema):
    category: Optional[str] = None

class SubCategoryFilterSchema(Schema):
    sub_category_id: Optional[str] = None

# NOTE: In Django-Ninja, you don't need a heavy custom Serializer subclass for query params!
# You can use this plain Schema block directly in your view like: `filters: StorefrontFilters = Query(...)`
class StorefrontFilters(Schema):
    """Parses optional URL query strings like ?subcategory=5 cleanly at the gateway layer."""
    subcategory: Optional[str] = None


# =========================================================================
# 1. INVENTORY & BRAND CATALOG SCHEMAS
# =========================================================================

class DropdownItemSchema(Schema):
    id: int
    name: str

class BrandCreateSchema(Schema):
    """Validates global or custom merchant brand registry models."""
    name: str
    slug: str
    sub_category_ids: List[int] = []

class InitialDropdownsResponseSchema(Schema):
    status: str = "success"
    sub_categories: List[DropdownItemSchema]
    brands: List[DropdownItemSchema]

class SubCategoryCreateSchema(Schema):
    """Validates fine-grained category niches under primary departments."""
    primary_category: CategoryLiteral
    name: str
    slug: str


# =========================================================================
# 2. VARIANT MATRIX INPUT SCHEMAS (FOR SKU COMBINATIONS)
# =========================================================================

class ProductVariantSKUInput(Schema):
    """Maps custom prices and stock counts to single variations."""
    value_ids: List[int]
    stock_qty: int
    price_override: Decimal | None = None
    is_active: bool = True


class ProductVariantOptionInput(Schema):
    """Maps structure option matrices e.g. Name='Size', Values=['M', 'L']"""
    name: str
    values: List[str]


class ProductImageInputSchema(Schema):
    """Maps custom metadata for an individual secondary gallery asset."""
    file_key: str              # e.g., "image_0", "image_1" to match multipart keys
    caption: str | None = None


# =========================================================================
# 3. CORE PRODUCT MANIFEST SCHEMAS
# =========================================================================

class ProductSKUItemSchema(Schema):
    """
    Validates incoming structural items inside the sku_items array payload.
    Ensures absolute data validation coverage for variant-level promotions.
    """
    id: Optional[int] = None       # Provided during edits, null during creation
    item_id: Optional[int] = None  # Alternative lookup fallback identifier
    stock_qty: int
    price_override: Decimal | None = None

    value_ids: Optional[List[int]] = None
    value_names: Optional[List[str]] = None  # Accepts ["8GB", "i5"] from Nuxt frontend
    sku: Optional[str] = None                # Accepts the frontend key text string gracefully
    
    # Promotional configuration parameters inside iteration rows
    bonanza_mode: bool = False
    discount_price: Decimal | None = None
    is_active: bool = True


class MenuOptionDetail(Schema):
    id: int
    name: str
    price: Decimal
    is_available: bool

class ProductSavePayloadSchema(Schema):
    """
    Validates inventory creations for Restaurants, Pharmacies, and Retailers.
    """
    name: str
    description: str
    price: Decimal
    category: CategoryLiteral

    key_features: Optional[str] = "[]"
    
    sub_category_id: Optional[str] = None
    new_sub_category_name: Optional[str] = None

    # Brand Controls
    brand_selection: str = "NONE"                      
    new_brand_name: Optional[str] = None       
    
    # Promotional Rulesets
    bonanza_mode: bool = False
    discount_price: Decimal | None = None
    is_available: bool = True

    image_key: Optional[str] = "image"
    
    # Explicit deep parsing schemas can be attached if these fields aren't raw JSON payloads anymore
    variant_options: Optional[Any] = None
    # variant_options: ProductVariantOptionInput = None
    # sku_items: Optional[Any] = None
    sku_items: Optional[Any] = None
    gallery_images: Optional[Any] = None
    # gallery_images: ProductImageInputSchema = None
    menu_options: Optional[str] = "[]"


class BatchSKUOverrideUpdateSchema(Schema):
    """Validates pricing/stock changes on the management matrix grid."""
    item_id: int
    stock_qty: int
    price_override: Decimal | None = None

    bonanza_mode: bool = False
    discount_price: Decimal | None = None

    is_active: bool = True


class VendorRatingSubmissionSchema(Schema):
    order_reference: str
    rating: int = Field(ge=1, le=5)

class ProductAddSuccessResponse(Schema):
    status: str = "success"
    message: str
    product_id: str
    image_url: Optional[str] = None
    
# =========================================================================
# 4. MARKETPLACE CHECKOUT & CART MANIFEST SCHEMAS
# =========================================================================

class ProductQuantityInput(Schema):
    """Parses explicit list elements from modern structured JSON payloads."""
    product_id: Optional[str] = None
    quantity: int = 1
    # price_at_order: float


class CheckoutRequestInput(Schema):
    """Validates unified checkout params directly from client apps."""
    delivery_address: str
    base_location: Optional[str] = Field(default="")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    items: List[ProductQuantityInput]


# =========================================================================
# 5. RESPONSE OUTBOUND SCHEMAS (DATA CASTING FILTERS)
# =========================================================================

class CheckoutRedirectResponse(Schema):
    status: str
    message: str = "Success"
    checkout_url: str
    order_id: str


class OrderSummaryItemResponse(Schema):
    product_id: str
    product_name: str
    quantity: int
    price_at_order: Decimal


class OrderSummaryResponse(Schema):
    status: str
    order_id: str
    vendor_name: str
    delivery_address: str
    item_total: Decimal | None = None
    delivery_charge: Decimal | None = None
    total_price: Decimal | None = None
    order_status: str
    items: List[OrderSummaryItemResponse]


class PublicVendorDetail(Schema):
    id: str
    company_name: str
    business_type: str
    is_currently_open: bool


class GalleryImageDetail(Schema):
    id: int
    image: str
    caption: str

class SwatchValueDetail(Schema):
    value_id: int
    value_text: str
    
class SwatchOptionDetail(Schema):
    option_id: int
    name: str
    values: List[SwatchValueDetail]

class SKUMatrixItemDetail(Schema):
    id: int
    sku: str
    base_price: Decimal
    current_price: Decimal
    discount_price: Optional[Decimal] = None
    bonanza_mode: bool
    discount_percentage: int
    stock_qty: int
    value_ids: List[int]

class PublicProductDetailResponse(Schema):
    id: str
    name: str
    description: str
    key_features: Any
    base_price: Decimal
    current_price: Decimal
    category: str
    sub_category: Optional[str] = None
    brand: Optional[str] = None
    bonanza_mode: bool
    discount_percentage: int
    image: Optional[str] = None
    is_available: bool
    vendor: PublicVendorDetail
    gallery_images: List[GalleryImageDetail]  # Reuses your previous GalleryImageDetail schema
    variant_options: List[SwatchOptionDetail]
    sku_matrix: List[SKUMatrixItemDetail]

class PublicProductPayloadSchema(Schema):
    status: str = "success"
    message: str
    data: PublicProductDetailResponse



