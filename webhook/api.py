import hmac
import hashlib
import json
import logging
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from ninja import Router
from marketplace.models import MarketplaceOrder
from wallet.utils import verify_order_payment
from wallet.services import finalize_payout
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

router = Router(tags=["Payment Webhooks"])


@router.post("/paystack")
async def paystack_webhook(request):
    """
    Server-to-server listener for Paystack events (signature-verified).
      - charge.success: confirms the order against Paystack's own records
        (reference, currency, amount) inside verify_order_payment.
      - transfer.success / failed / reversed: settles merchant payouts.
    Handlers are idempotent, so Paystack retries and duplicate deliveries are safe.
    """
    paystack_signature = request.headers.get('x-paystack-signature', '')
    body = request.body

    # 1. Signature validation
    secret = settings.PAYSTACK_SECRET_KEY.encode('utf-8')
    computed_signature = hmac.new(secret, body, hashlib.sha512).hexdigest()

    if not paystack_signature or not hmac.compare_digest(computed_signature, paystack_signature):
        return JsonResponse({"detail": "Invalid signature."}, status=400)

    # 2. Parse payload defensively
    try:
        event_data = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"detail": "Malformed payload."}, status=400)

    event = event_data.get('event')
    data = event_data.get('data') or {}

    # 3. Successful charges
    if event == 'charge.success':
        reference = data.get('reference')
        if not reference:
            return HttpResponse("NO REFERENCE", status=200)

        # Strictly by the reference stored at checkout. Metadata is never trusted.
        order = await MarketplaceOrder.objects.filter(payment_reference=reference).afirst()
        if not order:
            logger.warning("Paystack charge.success for unknown reference %s", reference)
            return HttpResponse("ORDER NOT FOUND", status=200)  # not ours: stop retries

        success = await sync_to_async(verify_order_payment)(
            str(order.id), transaction_id=reference
        )
        if success:
            logger.info("Order %s confirmed via Paystack webhook (ref %s)", order.id, reference)
            return HttpResponse("WEBHOOK RECEIVED", status=200)

        await order.arefresh_from_db()

        if order.status == MarketplaceOrder.OrderStatus.CANCELLED:
            # Customer paid after the order was cancelled/released. Retrying cannot fix
            # this: a human must refund it from the Paystack dashboard (or you re-open it).
            logger.critical(
                "PAID CANCELLED ORDER: order %s, reference %s. Refund required.",
                order.id, reference,
            )
            return HttpResponse("ORDER CANCELLED", status=200)

        # Still PENDING: verification failed or Paystack was unreachable. Non-2xx makes
        # Paystack redeliver later; the endpoint is idempotent so retries are harmless.
        logger.warning("Order %s: charge.success did not verify yet (ref %s)", order.id, reference)
        return HttpResponse("VERIFICATION PENDING", status=503)

    # 4. Merchant payout settlement
    if event in ("transfer.success", "transfer.failed", "transfer.reversed"):
        reference = data.get("reference")
        if reference:
            outcome = "success" if event == "transfer.success" else "failed"
            try:
                await sync_to_async(finalize_payout)(
                    reference, outcome,
                    transfer_code=data.get("transfer_code"),
                    reason=data.get("reason") or event,
                )
            except Exception:
                logger.exception("Failed to settle payout %s on %s", reference, event)
                return HttpResponse("RETRY", status=500)   # DB hiccup: let Paystack retry

    return HttpResponse("WEBHOOK RECEIVED", status=200)