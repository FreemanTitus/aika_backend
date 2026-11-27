from decimal import Decimal


def _is_order_received_by_customer(order) -> bool:
    """
    Returns True once the customer has confirmed receipt (clicked received).
    Adjust field/status checks to match your MarketplaceOrder model.
    """
    status = str(getattr(order, "status", "") or "").strip().lower()
    if status in {"received", "completed", "settled", "released"}:
        return True
    if bool(getattr(order, "is_received", False) or getattr(order, "customer_confirmed_receipt", False)):
        return True
    if getattr(order, "received_at", None) or getattr(order, "customer_confirmed_at", None):
        return True
    return False


def _is_order_paid(order) -> bool:
    """
    Returns True if payment has been confirmed for the order.
    """
    status = str(getattr(order, "status", "") or "").strip().lower()
    if bool(getattr(order, "is_paid", False) or getattr(order, "paid_at", None)):
        return True
    return status in {"paid", "processing", "out_for_delivery", "delivered", "received", "completed", "settled", "released"}


def build_vendor_wallet_transactions(vendor_profile, limit: int = 50):
    """
    Builds a unified transaction list from paid/received orders without modifying
    VendorWallet or existing order/payment logic.
    """
    orders = (
        vendor_profile.vendor_orders
        .select_related("customer__user")
        .order_by("-created_at")[:limit]
    )

    tx_list = []
    for order in orders:
        if not _is_order_paid(order):
            continue

        received = _is_order_received_by_customer(order)
        # Use vendor net/item amount if you have one, otherwise subtotal
        amount = Decimal(str(getattr(order, "vendor_payout", None) or getattr(order, "subtotal", None) or order.total_price or 0))

        customer_user = getattr(getattr(order, "customer", None), "user", None)
        buyer_label = (
            f"{getattr(customer_user, 'first_name', '')} {getattr(customer_user, 'last_name', '')}".strip()
            or getattr(customer_user, "email", None)
            or "Customer"
        )

        tx_list.append({
            "id": f"ord-{order.id}",
            "order_id": order.id,
            "reference": getattr(order, "payment_reference", None) or getattr(order, "reference", None) or f"ORD-{order.id}",
            "title": f"Order #{order.id} · {buyer_label}",
            "amount": float(amount),
            # Shows as "pending" while in escrow before customer clicks received
            "status": "completed" if received else "pending",
            "balance_bucket": "available" if received else "escrow",
            "status_label": "Available" if received else "Pending (In Escrow)",
            "created_at": order.created_at.isoformat() if getattr(order, "created_at", None) else None,
        })

    return tx_list