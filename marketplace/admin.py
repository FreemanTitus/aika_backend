# admin.py
from django.contrib import admin, messages
from django.utils.html import format_html
from .models import (
    SubCategory,
    Brand,
    Product,
    ProductImage,
    VariantOption,
    VariantValue,
    ProductItem,
    MenuItemOption,
    MarketplaceOrder,
    MarketplaceOrderItem,
)


# ─────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────

class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ('image', 'image_preview', 'caption', 'created_at')
    readonly_fields = ('image_preview', 'created_at')

    @admin.display(description='Preview')
    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{0}" style="height:56px; width:56px; object-fit:cover; border-radius:6px;" />',
                obj.image.url,
            )
        return "—"


class VariantOptionInline(admin.TabularInline):
    model = VariantOption
    extra = 1
    fields = ('name',)


class MenuItemOptionInline(admin.TabularInline):
    model = MenuItemOption
    extra = 1
    fields = ('name', 'price', 'is_available')


class MarketplaceOrderItemInline(admin.TabularInline):
    model = MarketplaceOrderItem
    extra = 0
    can_delete = False
    fields = ('product', 'product_item', 'quantity', 'price_at_order', 'line_total')
    readonly_fields = ('product', 'product_item', 'quantity', 'price_at_order', 'line_total')
    autocomplete_fields = ('product', 'product_item')

    @admin.display(description='Line Total')
    def line_total(self, obj):
        if obj.pk:
            return f"₦{obj.get_total_item_price():,.2f}"
        return "—"

    def has_add_permission(self, request, obj=None):
        return False


# ─────────────────────────────────────────────────────────────
# Catalog & Taxonomy Admins
# ─────────────────────────────────────────────────────────────

@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'primary_category', 'slug')
    list_filter = ('primary_category',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    ordering = ('primary_category', 'name')


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'created_by', 'sub_category_list')
    list_filter = ('sub_categories',)
    search_fields = ('name', 'slug', 'created_by__email')
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ('sub_categories',)
    autocomplete_fields = ('created_by',)
    ordering = ('name',)

    @admin.display(description='Sub-Categories')
    def sub_category_list(self, obj):
        names = obj.sub_categories.values_list('name', flat=True)
        return ", ".join(names) if names else "Global / All"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'thumbnail',
        'name',
        'vendor',
        'category',
        'sub_category',
        'brand',
        'price',
        'bonanza_mode',
        'discount_price',
        'is_available',
    )
    list_filter = ('category', 'sub_category', 'brand', 'is_available', 'bonanza_mode', 'vendor')
    list_editable = ('price', 'bonanza_mode', 'discount_price', 'is_available')
    search_fields = ('id', 'name', 'description', 'vendor__company_name', 'brand__name', 'sub_category__name')
    readonly_fields = ('id', 'image_preview', 'effective_price_display', 'discount_percentage_display')
    autocomplete_fields = ('vendor', 'sub_category', 'brand')
    inlines = [ProductImageInline, VariantOptionInline, MenuItemOptionInline]
    ordering = ('name',)
    actions = ['mark_available', 'mark_unavailable', 'enable_bonanza', 'disable_bonanza']

    fieldsets = (
        ('Product Identity & Vendor', {
            'fields': ('id', 'vendor', 'name', 'description', 'key_features')
        }),
        ('Categorization', {
            'fields': ('category', 'sub_category', 'brand')
        }),
        ('Pricing, Discounts & Stock', {
            'fields': (
                ('price', 'effective_price_display', 'discount_percentage_display'),
                ('bonanza_mode', 'discount_price'),
                'is_available',
            )
        }),
        ('Primary Media', {
            'fields': ('image', 'image_preview')
        }),
    )

    @admin.display(description='Img')
    def thumbnail(self, obj):
        if obj.image:
            return format_html(
                '<img src="{0}" style="height:40px; width:40px; object-fit:cover; border-radius:6px;" />',
                obj.image.url,
            )
        return "—"

    @admin.display(description='Primary Image Preview')
    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:140px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.image.url,
            )
        return "No primary image uploaded"

    @admin.display(description='Active Price')
    def effective_price_display(self, obj):
        return f"₦{obj.current_price:,.2f}"

    @admin.display(description='Discount')
    def discount_percentage_display(self, obj):
        return f"{obj.discount_percentage}%"

    @admin.action(description="Mark selected products as available")
    def mark_available(self, request, queryset):
        updated = queryset.update(is_available=True)
        self.message_user(request, f"{updated} product(s) marked available.", level=messages.SUCCESS)

    @admin.action(description="Mark selected products as unavailable")
    def mark_unavailable(self, request, queryset):
        updated = queryset.update(is_available=False)
        self.message_user(request, f"{updated} product(s) marked unavailable.", level=messages.WARNING)

    @admin.action(description="Enable bonanza mode on selected products")
    def enable_bonanza(self, request, queryset):
        updated = queryset.update(bonanza_mode=True)
        self.message_user(request, f"Bonanza mode enabled for {updated} product(s).", level=messages.SUCCESS)

    @admin.action(description="Disable bonanza mode on selected products")
    def disable_bonanza(self, request, queryset):
        updated = queryset.update(bonanza_mode=False)
        self.message_user(request, f"Bonanza mode disabled for {updated} product(s).", level=messages.INFO)


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ('id', 'product', 'image_preview', 'caption', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('product__name', 'product__id', 'caption')
    readonly_fields = ('created_at', 'image_preview')
    autocomplete_fields = ('product',)
    ordering = ('-created_at',)

    @admin.display(description='Preview')
    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:120px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.image.url,
            )
        return "No image"


@admin.register(VariantOption)
class VariantOptionAdmin(admin.ModelAdmin):
    list_display = ('name', 'product')
    search_fields = ('name', 'product__name', 'product__id')
    autocomplete_fields = ('product',)
    ordering = ('product__name', 'name')


@admin.register(VariantValue)
class VariantValueAdmin(admin.ModelAdmin):
    list_display = ('value', 'option', 'product_name')
    list_filter = ('option__name',)
    search_fields = ('value', 'option__name', 'option__product__name')
    autocomplete_fields = ('option',)
    ordering = ('option__product__name', 'option__name', 'value')

    @admin.display(description='Product', ordering='option__product__name')
    def product_name(self, obj):
        return obj.option.product.name


@admin.register(ProductItem)
class ProductItemAdmin(admin.ModelAdmin):
    list_display = (
        'sku',
        'product',
        'variant_summary',
        'base_selling_price_display',
        'current_price_display',
        'stock_qty',
        'bonanza_mode',
        'is_active',
    )
    list_filter = ('is_active', 'bonanza_mode', 'product__category')
    list_editable = ('stock_qty', 'bonanza_mode', 'is_active')
    search_fields = ('sku', 'product__name', 'product__id', 'variant_values__value')
    readonly_fields = ('base_selling_price_display', 'current_price_display', 'discount_percentage_display')
    autocomplete_fields = ('product',)
    filter_horizontal = ('variant_values',)
    ordering = ('product__name', 'id')

    fieldsets = (
        ('SKU & Product', {
            'fields': ('product', 'sku', 'variant_values', 'is_active')
        }),
        ('Inventory & Pricing', {
            'fields': (
                'stock_qty',
                ('price_override', 'base_selling_price_display'),
                ('bonanza_mode', 'discount_price', 'current_price_display', 'discount_percentage_display'),
            )
        }),
    )

    @admin.display(description='Variants')
    def variant_summary(self, obj):
        vals = obj.variant_values.values_list('value', flat=True)
        return ", ".join(vals) if vals else "Base Model"

    @admin.display(description='Base Price')
    def base_selling_price_display(self, obj):
        return f"₦{obj.base_selling_price:,.2f}"

    @admin.display(description='Active Price')
    def current_price_display(self, obj):
        return f"₦{obj.current_price:,.2f}"

    @admin.display(description='Discount')
    def discount_percentage_display(self, obj):
        return f"{obj.discount_percentage}%"


@admin.register(MenuItemOption)
class MenuItemOptionAdmin(admin.ModelAdmin):
    list_display = ('name', 'product', 'price', 'is_available')
    list_filter = ('is_available', 'product__vendor')
    list_editable = ('price', 'is_available')
    search_fields = ('name', 'product__name', 'product__vendor__company_name')
    autocomplete_fields = ('product',)
    ordering = ('product__name', 'name')


# ─────────────────────────────────────────────────────────────
# Orders Admins
# ─────────────────────────────────────────────────────────────

@admin.register(MarketplaceOrder)
class MarketplaceOrderAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'vendor',
        'customer',
        'status',
        'fulfillment_mode',
        'subtotal',
        'delivery_fee',
        'wallet_applied',
        'total_price',
        'created_at',
    )
    list_filter = ('status', 'fulfillment_mode', 'created_at')
    search_fields = (
        'id',
        'payment_reference',
        'vendor__company_name',
        'customer__first_name',
        'customer__last_name',
        'customer__user__email',
        'delivery_address',
    )
    readonly_fields = ('id', 'created_at', 'vendor_marked_delivered_at')
    autocomplete_fields = ('customer', 'vendor', 'logistics_dispatch')
    inlines = [MarketplaceOrderItemInline]
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = ['mark_processing', 'mark_paid_unassigned', 'mark_in_transit', 'mark_completed', 'mark_cancelled']

    fieldsets = (
        ('Order Reference & Status', {
            'fields': ('id', 'status', 'fulfillment_mode', 'logistics_dispatch', 'vendor_marked_delivered_at')
        }),
        ('Parties & Delivery Destination', {
            'fields': ('vendor', 'customer', 'base_location', 'delivery_address')
        }),
        ('Financials & Payment', {
            'fields': (
                ('subtotal', 'delivery_fee', 'wallet_applied', 'total_price'),
                'payment_reference',
                'payment_checkout_url',
            )
        }),
        ('Timestamps', {
            'fields': ('created_at',)
        }),
    )

    @admin.action(description="Mark selected orders as Processing")
    def mark_processing(self, request, queryset):
        updated = queryset.update(status=MarketplaceOrder.OrderStatus.PROCESSING)
        self.message_user(request, f"{updated} order(s) set to Processing.", level=messages.INFO)

    @admin.action(description="Mark selected orders as Paid (Awaiting Rider Pickup)")
    def mark_paid_unassigned(self, request, queryset):
        updated = queryset.update(status=MarketplaceOrder.OrderStatus.PAID_UNASSIGNED)
        self.message_user(request, f"{updated} order(s) set to Paid (Awaiting Rider Pickup).", level=messages.SUCCESS)

    @admin.action(description="Mark selected orders as In Transit")
    def mark_in_transit(self, request, queryset):
        updated = queryset.update(status=MarketplaceOrder.OrderStatus.IN_TRANSIT)
        self.message_user(request, f"{updated} order(s) set to In Transit.", level=messages.INFO)

    @admin.action(description="Mark selected orders as Completed")
    def mark_completed(self, request, queryset):
        updated = queryset.update(status=MarketplaceOrder.OrderStatus.COMPLETED)
        self.message_user(request, f"{updated} order(s) marked Completed.", level=messages.SUCCESS)

    @admin.action(description="Mark selected orders as Cancelled")
    def mark_cancelled(self, request, queryset):
        updated = queryset.update(status=MarketplaceOrder.OrderStatus.CANCELLED)
        self.message_user(request, f"{updated} order(s) marked Cancelled.", level=messages.WARNING)


@admin.register(MarketplaceOrderItem)
class MarketplaceOrderItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'item_name_display', 'quantity', 'price_at_order', 'line_total')
    search_fields = ('order__id', 'product__name', 'product_item__sku', 'product_item__product__name')
    readonly_fields = ('order', 'product', 'product_item', 'quantity', 'price_at_order', 'line_total')
    ordering = ('-order__created_at', 'id')

    @admin.display(description='Item')
    def item_name_display(self, obj):
        return obj.get_product_name

    @admin.display(description='Total')
    def line_total(self, obj):
        return f"₦{obj.get_total_item_price():,.2f}"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False