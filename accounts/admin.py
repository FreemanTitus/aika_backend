# admin.py
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from .models import State, CoverageArea, CustomUser, CustomerReview


class CoverageAreaInline(admin.TabularInline):
    model = CoverageArea
    extra = 1
    fields = ('name', 'latitude', 'longitude', 'is_active')


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ('name', 'hub_count')
    search_fields = ('name',)
    ordering = ('name',)
    inlines = [CoverageAreaInline]

    @admin.display(description='Coverage Areas')
    def hub_count(self, obj):
        return obj.coverage_areas.count()


@admin.register(CoverageArea)
class CoverageAreaAdmin(admin.ModelAdmin):
    list_display = ('name', 'state', 'latitude', 'longitude', 'is_active')
    list_filter = ('state', 'is_active')
    list_editable = ('is_active',)
    search_fields = ('name', 'state__name')
    ordering = ('state__name', 'name')
    actions = ['geocode_selected_areas']

    @admin.action(description="Geocode selected coverage areas via Nominatim")
    def geocode_selected_areas(self, request, queryset):
        succeeded = 0
        failed = 0
        for area in queryset.select_related('state'):
            if area.geocode(save=True):
                succeeded += 1
            else:
                failed += 1
        self.message_user(
            request,
            f"Geocoding complete: {succeeded} updated, {failed} failed.",
            level=messages.SUCCESS if failed == 0 else messages.WARNING,
        )


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    model = CustomUser
    list_display = (
        'email',
        'first_name',
        'last_name',
        'role',
        'phone_number',
        'base_location',
        'is_active',
        'is_staff',
        'date_joined',
    )
    list_filter = ('role', 'is_active', 'is_staff', 'is_superuser', 'is_terms_accepted', 'base_location__state')
    search_fields = ('email', 'first_name', 'last_name', 'phone_number', 'street_address')
    ordering = ('-date_joined',)
    readonly_fields = ('date_joined', 'last_login', 'terms_accepted_at', 'token_version')
    autocomplete_fields = ('base_location',)

    fieldsets = (
        ('Authentication', {
            'fields': ('email', 'password', 'token_version')
        }),
        ('Personal & Contact Info', {
            'fields': ('first_name', 'last_name', 'phone_number', 'role')
        }),
        ('Location & Dispatch Zone', {
            'fields': ('base_location', 'street_address', 'latitude', 'longitude')
        }),
        ('Legal & Compliance', {
            'fields': ('is_terms_accepted', 'terms_accepted_at')
        }),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')
        }),
        ('Important Dates', {
            'fields': ('last_login', 'date_joined')
        }),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email',
                'first_name',
                'last_name',
                'phone_number',
                'role',
                'base_location',
                'password1',
                'password2',
                'is_staff',
                'is_active',
            ),
        }),
    )


@admin.register(CustomerReview)
class CustomerReviewAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'short_comment', 'is_approved', 'created_at')
    list_filter = ('is_approved', 'created_at')
    list_editable = ('is_approved',)
    search_fields = ('name', 'role', 'comment')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)
    actions = ['approve_reviews', 'unapprove_reviews']

    @admin.display(description='Comment')
    def short_comment(self, obj):
        return (obj.comment[:60] + '...') if len(obj.comment) > 60 else obj.comment

    @admin.action(description="Approve selected reviews for homepage")
    def approve_reviews(self, request, queryset):
        updated = queryset.update(is_approved=True)
        self.message_user(request, f"{updated} review(s) approved.", level=messages.SUCCESS)

    @admin.action(description="Unapprove selected reviews")
    def unapprove_reviews(self, request, queryset):
        updated = queryset.update(is_approved=False)
        self.message_user(request, f"{updated} review(s) hidden.", level=messages.INFO)