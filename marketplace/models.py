import math
import os
from django.db import models
from django.conf import settings
from customers.models import IndividualProfile
from merchant.models import MerchantProfile
from order.models import Order
from accounts.utils import generate_alphanumeric_id
import random


def product_gallery_upload_path(instance, filename):
    """
    Generates a structured, clean path for secondary product gallery images.
    Example output: product_gallery/wf-prod-123456/filename.jpg
    """
    return os.path.join('product_gallery', f"wf-prod-{instance.product.id}", filename)

def product_image_upload_path(instance, filename):
    """
    Organizes uploads dynamically into: media/products/vendor_id/filename
    """
    ext = filename.split('.')[-1]
    filename = f"{instance.name.slugify() if hasattr(instance.name, 'slugify') else instance.name}_{instance.id}.{ext}"
    return os.path.join('products', str(instance.vendor.id), filename)


class VariantOption(models.Model):
    """
    Defines the name of the variant category.
    Examples: 'Color', 'Size', 'Shoe Size'
    """
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='variant_options')
    name = models.CharField(max_length=50) # e.g., "Size" or "Color"

    def __str__(self):
        return f"{self.product.name} - {self.name}"


class VariantValue(models.Model):
    """
    The actual values belonging to a variation category.
    Examples: 'Red', 'Blue', 'XL', 'L', 'M'
    """
    option = models.ForeignKey(VariantOption, on_delete=models.CASCADE, related_name='values')
    value = models.CharField(max_length=50) # e.g., "XL" or "Red"

    def __str__(self):
        return f"{self.option.name}: {self.value}"


class Brand(models.Model):
    """
    Stores brands that can be added globally by admins or custom-created by vendors.
    """
    name = models.CharField(max_length=100, help_text="e.g., Apple, Tecno, Samsung, Nike")
    slug = models.SlugField(max_length=100, unique=True)
    
    # Optional subcategory link so brands are context-aware (e.g., Smartphones -> Tecno)
    sub_categories = models.ManyToManyField(
        'SubCategory', 
        related_name='brands',
        blank=True,
        help_text="The subcategories this brand belongs to."
    )
    
    # Null means it's a global system brand available to everyone
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='custom_brands',
        help_text="The merchant who created this brand. Leave blank for global access."
    )

    class Meta:
        ordering = ['name']
        unique_together = ('name', 'created_by') # Prevents a single merchant duplicate entry

    def __str__(self):
        if self.created_by:
            return f"{self.name} (Custom)"
        return self.name
    

class Product(models.Model):
    """
    Generalized inventory model used by Restaurants (Food/Drinks), 
    Pharmacies (Drugs), and E-Commerce vendors (Clothes, Tech, etc.)
    """
    class CategoryChoices(models.TextChoices):
        GADGETS = 'gadgets', 'Gadgets & Tech'
        APPAREL = 'apparel', 'Clothing & Apparel'
        HOME_APPLIANCES = 'appliances', 'Home Appliances'
        BEAUTY = 'beauty', 'Beauty & Cosmetics'
        RESTAURANT = 'restaurant', 'Restaurant'
        GENERAL = 'general', 'General Items'

    id = models.CharField(primary_key=True, max_length=15, editable=False)
    vendor = models.ForeignKey(
        'merchant.MerchantProfile',  # Ensure string matching for multi-tenant setups
        on_delete=models.CASCADE, 
        related_name='products'
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, help_text="Product specs, features, dosage, or menu details.")
    key_features = models.JSONField(
        default=list, 
        blank=True, 
        help_text="Stores structured parameters lists mapping keys down the storefront layout view nodes."
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image = models.ImageField(upload_to='product_images/', blank=True, null=True) # Fallback simple path or use yours
    
    category = models.CharField(max_length=100, choices=CategoryChoices.choices, default=CategoryChoices.GENERAL)
    sub_category = models.ForeignKey('SubCategory', on_delete=models.SET_NULL, blank=True, null=True, related_name='products')
    brand = models.ForeignKey('Brand', on_delete=models.SET_NULL, blank=True, null=True, related_name='products')
    
    bonanza_mode = models.BooleanField(default=False)
    discount_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_available = models.BooleanField(default=True, help_text="Turn off if item is out of stock")
    
    @property
    def is_orderable(self):
        """An item is orderable ONLY if the stock is active and the merchant platform is active/open."""
        # Fixed: Changed fallback context mapping from self.restaurant back to self.vendor
        return self.is_available and hasattr(self.vendor, 'is_currently_open') and self.vendor.is_currently_open

    @property
    def current_price(self):
        """Returns the actual operational price for checkout operations."""
        if self.bonanza_mode and self.discount_price and self.discount_price < self.price:
            return self.discount_price
        return self.price

    @property
    def discount_percentage(self):
        """Calculates the exact integer percentage removed for the badge UI."""
        if self.bonanza_mode and self.discount_price and self.discount_price < self.price:
            savings = self.price - self.discount_price
            percentage = (savings / self.price) * 100
            return math.floor(percentage)
        return 0

    def save(self, *args, **kwargs):
        if not self.id:
            # Assumes generate_alphanumeric_id exists in your module utils
            from django.utils.crypto import get_random_string
            while True:
                new_id = f"PRD-{generate_alphanumeric_id(8)}"
                if not Product.objects.filter(id=new_id).exists():
                    self.id = new_id
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - ₦{self.price} ({self.vendor.company_name})"


class ProductImage(models.Model):
    """
    Houses secondary image assets forming an endless sliding carousel/gallery 
    for a single corporate product manifest.
    """
    product = models.ForeignKey(
        Product, 
        on_delete=models.CASCADE, 
        related_name='images' # Access via product.images.all()
    )
    image = models.ImageField(
        upload_to=product_gallery_upload_path,
        help_text="Secondary gallery image asset for this product."
    )
    caption = models.CharField(
        max_length=150, 
        blank=True, 
        help_text="Optional alt text or caption (e.g., 'Side View', 'Back View')"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Gallery Asset for {self.product.name} (#{self.id})"


class ProductItem(models.Model):
    """
    The actual inventory unit (SKU) that a user purchases. 
    Tracks unique pricing and stock volumes for specific variant combinations.
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='items')
    sku = models.CharField(max_length=64, unique=True, blank=True, null=True)
    price_override = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        blank=True, 
        null=True, 
        help_text="Set if this variant combination costs more or less than the base product markdown."
    )

    bonanza_mode = models.BooleanField(default=False, help_text="Enable discount exclusively for this variant.")
    discount_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Promotional markdown price.")

    stock_qty = models.IntegerField(default=0)
    variant_values = models.ManyToManyField(VariantValue, related_name='product_items')
    is_active = models.BooleanField(default=True)

    def __str__(self):
        # We process this dynamically on evaluations
        vals = ", ".join([v.value for v in self.variant_values.all()])
        return f"{self.product.name} ({vals if vals else 'Base Model'}) - SKU: {self.sku or 'N/A'}"

    @property
    def base_selling_price(self):
        """Resolves the structural starting retail price before promotional logic runs."""
        if self.price_override is not None:
            return self.price_override
        return self.product.price

    @property
    def current_price(self):
        """
        Calculates checkout execution costs. Prioritizes variant-level discounts 
        over parent product global markdowns.
        """
        # 1. Variant-specific discount logic check (e.g., Core i5 is discounted, Core i7 is not)
        if self.bonanza_mode and self.discount_price and self.discount_price < self.base_selling_price:
            return self.discount_price

        # 2. Fallback to parent product global markdown ONLY if variant has no explicit settings
        if self.product.bonanza_mode and self.product.discount_price:
            if self.price_override is not None:
                discount_ratio = self.product.discount_price / self.product.price
                calculated_discount = self.base_selling_price * discount_ratio
                return calculated_discount.quantize(self.base_selling_price)
            return self.product.discount_price
                
        return self.base_selling_price

    @property
    def discount_percentage(self):
        """Calculates exact discount percentage for the variant UI badge."""
        base = self.base_selling_price
        curr = self.current_price
        if curr < base and base > 0:
            return math.floor(((base - curr) / base) * 100)
        return 0

    @property
    def dynamic_discount_price(self):
        """Helper to safely pass promotional rates to JSON SKU Matrix matrices."""
        if self.product.bonanza_mode:
            return self.current_price
        return None

    def save(self, *args, **kwargs):
        """Auto-compiles clean unique text tags for warehouse management fields if blank."""
        super().save(*args, **kwargs) # Save initial record to allow M2M access if processing tracking strings
        if not self.sku and self.id:
            # Compiles a readable tag: PRD-XXXXXX-VALUE1-VALUE2
            val_slugs = "-".join([v.value.replace(" ", "").upper() for v in self.variant_values.all()])
            self.sku = f"{self.product.id}-{val_slugs or 'BASE'}-{self.id}"
            ProductItem.objects.filter(id=self.id).update(sku=self.sku)


class MenuItemOption(models.Model):
    """
    Simple pricing tiers or options for food items 
    (e.g., Chicken Size: Small, Medium, Large with respective prices).
    """
    product = models.ForeignKey(
        'Product', 
        on_delete=models.CASCADE, 
        related_name='menu_options',
        limit_choices_to={'category__in': ['restaurant']}
    )
    name = models.CharField(max_length=100, help_text="e.g., 'Big Piece', 'Full Portion', 'Extra Spicy'")
    price = models.DecimalField(max_digits=10, decimal_places=2, help_text="The exact price for this specific menu option.")
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.product.name} - {self.name} (₦{self.price})"

    
class SubCategory(models.Model):
    """
    Dynamic model to store fine-grained niches mapping back to primary Category tiers.
    Example: primary_category='gadgets' -> name='Smartphones'
    """
    # Links directly back to your existing category choice tokens
    primary_category = models.CharField(
        max_length=50,
        choices=Product.CategoryChoices.choices,
        help_text="The parent department this subcategory belongs to."
    )
    name = models.CharField(max_length=100, help_text="e.g., Smartphones, Laptops, Burgers, Hoodies")
    slug = models.SlugField(max_length=100, unique=True, help_text="Used for clean SEO marketplace routing URLs")

    class Meta:
        verbose_name = "Sub-Category"
        verbose_name_plural = "Sub-Categories"
        ordering = ['name']

    def __str__(self):
        return f"{self.get_primary_category_display()} -> {self.name}"


class MarketplaceOrder(models.Model):
    class OrderStatus(models.TextChoices):
        PENDING = 'pending', 'Pending Payment'
        PROCESSING = 'processing', 'Processing/Preparing'
        PAID_UNASSIGNED = 'paid_unassigned', 'Paid (Awaiting Rider Pickup)'
        IN_TRANSIT = 'in_transit', 'In Transit'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        
    id = models.CharField(primary_key=True, max_length=20, editable=False)
    customer = models.ForeignKey(IndividualProfile, on_delete=models.CASCADE, related_name='marketplace_orders', null=True, blank=True)
    vendor = models.ForeignKey(MerchantProfile, on_delete=models.CASCADE, related_name='vendor_orders')
    
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    base_location = models.CharField(max_length=50, null=True, blank=True)
    delivery_address = models.TextField()
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING)

    payment_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    wallet_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    payment_checkout_url = models.URLField(max_length=500, null=True, blank=True)

    class FulfillmentMode(models.TextChoices):
        PLATFORM_DISPATCH = 'platform_dispatch', 'Platform Rider Dispatch'
        VENDOR_SELF_DELIVERY = 'vendor_self_delivery', 'Vendor Self-Delivery'

    fulfillment_mode = models.CharField(
        max_length=30, choices=FulfillmentMode.choices, default=FulfillmentMode.PLATFORM_DISPATCH
    )
    # Set when the vendor reports a self-delivered order as handed over; the customer still
    # confirms (or it auto-confirms after SELF_DELIVERY_AUTO_CONFIRM_HOURS).
    vendor_marked_delivered_at = models.DateTimeField(null=True, blank=True)
    
    #Connects this retail order directly to its logistics tracking entry
    logistics_dispatch = models.OneToOneField(
        Order, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='marketplace_source'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        """Generates a secure tracking key before storing the record instance."""
        if not self.id:
            while True:
                # Generates a human-scannable format like: A38F7E2K
                potential_id = f"{generate_alphanumeric_id(12)}"
                if not MarketplaceOrder.objects.filter(id=potential_id).exists():
                    self.id = potential_id
                    break
        super().save(*args, **kwargs)

    def calculate_totals(self, delivery_charge):
        from decimal import Decimal
        
        # 1. Sum up values using the relationship
        self.subtotal = sum(
            (item.get_total_item_price() for item in self.items.all()), 
            Decimal('0.00')
        )
        
        # 2. Set the fee and total
        self.delivery_fee = Decimal(str(delivery_charge))
        self.total_price = self.subtotal + self.delivery_fee
        
        # 3. Save only once with update_fields to avoid recursion
        self.save(update_fields=['subtotal', 'delivery_fee', 'total_price'])

    @property
    def subtotal_value(self):
        # Use a property instead of get_subtotal() for cleaner template access
        from decimal import Decimal
        return sum(
            (item.get_total_item_price() for item in self.items.all()), 
            Decimal('0.00')
        )

    def __str__(self):
        return f"Marketplace Order #{self.id} via {self.vendor.company_name} (₦{self.total_price})"
    
    
class MarketplaceOrderItem(models.Model):
    order = models.ForeignKey(MarketplaceOrder, on_delete=models.CASCADE, related_name='items')
    product_item = models.ForeignKey('marketplace.ProductItem', on_delete=models.PROTECT, related_name='order_items', null=True, blank=True)
    product = models.ForeignKey('marketplace.Product', on_delete=models.PROTECT, related_name='order_items', null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    price_at_order = models.DecimalField(max_digits=10, decimal_places=2, help_text="Snapshots price to maintain historical invoice accuracy")

    def get_total_item_price(self):
        return self.price_at_order * self.quantity

    @property
    def get_product_name(self):
        """
        Resolves the display name from whichever link exists. It no longer walks
        order -> vendor on every row (two extra queries per item) and no longer crashes
        with AttributeError when product or product_item is NULL.
        """
        if self.product_id and self.product:
            return self.product.name
        if self.product_item_id and self.product_item and self.product_item.product_id:
            return self.product_item.product.name
        return "Item"

    def __str__(self):
        # Resolve name from whichever link exists
        name = self.product.name if self.product else (self.product_item.product.name if self.product_item else "Unknown")
        return f"{self.quantity}x {name} in Order #{self.order.id}"
    
