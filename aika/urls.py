from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from ninja import NinjaAPI
from accounts.api import router as accounts_router
from booking.api import router as booking_router
from marketplace.api import router as marketplace_router
from merchant.api import router as merchant_router
from wallet.api import router as wallet_router
from customers.api import router as customers_router
from order.api import router as order_router
from rider.api import router as rider_router
from admins.api import router as admin_router
from payment.api import router as bill_router
from webhook.api import router as webhook_router
from staff.api import router as staff_router
from marketing.api import router as marketing_router
from logistics.api import router as logistic_router
from publics.api import router as public_router
from messaging.api import router as messaging_router
from support.api import router as support_router
from cart.api import router as cart_router

from django.contrib.admin.views.decorators import staff_member_required





api = NinjaAPI(
    title="Aika API", 
    version="1.0.0",
    docs_decorator=staff_member_required
)
# Register the accounts router with the main API instance
api.add_router("auth/", accounts_router)
api.add_router("admin/", admin_router)
api.add_router("bookings/", booking_router)
api.add_router("marketplace/", marketplace_router)
api.add_router("merchant/", merchant_router)
api.add_router("wallet/", wallet_router)
api.add_router("customer/", customers_router)
api.add_router("orders/", order_router)
api.add_router("rider/", rider_router)
api.add_router("payment/", bill_router)
api.add_router("webhook/", webhook_router)
api.add_router("staff/", staff_router)  # Register the agent router
api.add_router("marketing/", marketing_router)  # Register the marketing router
api.add_router("logistics/", logistic_router) 
api.add_router("public/", public_router)
api.add_router("support/", messaging_router)
api.add_router("agent/", support_router)
api.add_router("cart/", cart_router)


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', api.urls),
    path('webpush/', include('webpush.urls')),  # the frontend posts push subscriptions here
]


if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += [path('silk/', include('silk.urls', namespace='silk'))]