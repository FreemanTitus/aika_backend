from ninja import Router, Schema
from django.shortcuts import get_object_or_404
from django.db import transaction
from ninja.errors import HttpError
from accounts.utils import JWTAuth
from messaging.models import Conversation, Message
from channels.layers import get_channel_layer
from asgiref.sync import sync_to_async
from .schemas import ReplySchema
from messaging.task import close_idle_chats
from django.db.models import Q
from order.models import Order
from marketplace.models import MarketplaceOrder, MarketplaceOrderItem
from accounts.security import RequireRole


router = Router(tags=["Agent Support Queue"], auth=RequireRole(allowed_roles=['agent']))



# 1. Get Open Queue (Unassigned customer chats) & Agent's Active Chats
@router.get("/queues")
def get_agent_queues(request):
    user = request.auth
    if user.role != 'agent':
        raise HttpError(403, "Access restricted to Support Agents.")

    # Unassigned incoming customer chats waiting for an agent
    unassigned_chats = Conversation.objects.filter(
        conversation_type='customer_support',
        assigned_staff__isnull=True,
        is_active=True
    ).select_related('customer').order_by('-created_at')

    # Chats currently claimed and handled by this specific agent
    my_active_chats = Conversation.objects.filter(
        conversation_type='customer_support',
        assigned_staff=user,
        is_active=True
    ).select_related('customer').order_by('-updated_at')

    # close_idle_chats()

    return {
        "status": "success",
        "unassigned_queue": [
            {
                "id": c.id,
                "customer_name": c.display_name,
                "customer_email": c.display_email,
                "created_at": c.created_at.isoformat()
            } for c in unassigned_chats
        ],
        "my_active_chats": [
            {
                "id": c.id,
                "customer_name": c.display_name,
                "customer_email": c.display_email,
                "updated_at": c.updated_at.isoformat()
            } for c in my_active_chats
        ]
    }

@router.post("/chat/{conv_id}/join")
async def join_customer_chat(request, conv_id: str):
    user = request.auth

    @sync_to_async
    def execute_join():
        with transaction.atomic():
            try:
                conv = Conversation.objects.select_for_update().get(
                    id=conv_id, conversation_type='customer_support'
                )
            except Conversation.DoesNotExist:
                raise HttpError(404, "Conversation not found.")

            if not conv.is_active:
                raise HttpError(400, "This conversation has already ended.")

            if conv.assigned_staff_id and conv.assigned_staff_id != user.id:
                raise HttpError(400, "This conversation has already been claimed by another agent.")

            conv.assigned_staff = user
            conv.save(update_fields=['assigned_staff', 'updated_at'])

            msg_content = "You are now connected with a support agent."

            # Save without assigning a specific user sender so it's a neutral system message
            new_msg = Message.objects.create(
                conversation=conv,
                sender=None,
                content=msg_content
            )
            return conv.id, new_msg.id, new_msg.created_at, msg_content

    conv_id_val, msg_id, msg_created_at, msg_content = await execute_join()

    # Broadcast via WebSocket to the chat room using native await
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"chat_{conv_id_val}",
            {
                "type": "chat_message",
                "id": msg_id,
                "content": msg_content,
                "sender_id": None,
                "sender_role": "system",
                "created_at": msg_created_at.isoformat(),
            }
        )

        # Update queue for all agents
        await channel_layer.group_send(
            "support_queue",
            {
                "type": "queue_update_broadcast",
                "action": "chat_claimed",
                "conversation_id": conv_id_val
            }
        )

    return {"status": "success", "message": "Successfully joined conversation."}

@router.get("/chat/{conv_id}/messages")
def get_chat_messages(request, conv_id: str):
    user = request.auth

    conv = get_object_or_404(Conversation, id=conv_id, conversation_type='customer_support')

    # Ensure the agent is assigned or has access
    if conv.assigned_staff_id != user.id and not (user.is_superuser or user.role == 'admin'):
        raise HttpError(403, "You are not assigned to this conversation.")

    messages = Message.objects.filter(conversation=conv).select_related('sender').order_by('created_at')

    return {
        "status": "success",
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender.id if m.sender else None,
                "sender_email": m.sender.email if m.sender else conv.display_email,
                "message": m.content,
                "created_at": m.created_at.isoformat()
            } for m in messages
        ]
    }

# 3. Agent sends a reply to an active customer chat
@router.post("/chat/{conv_id}/reply")
def agent_send_message(request, conv_id: str, payload: ReplySchema):
    user = request.auth

    conv = get_object_or_404(Conversation, id=conv_id, conversation_type='customer_support', assigned_staff=user)

    if not conv.is_active:
        raise HttpError(400, "Cannot reply to a closed conversation.")

    content = payload.content.strip()
    if not content:
        raise HttpError(400, "Message cannot be empty.")

    message = Message.objects.create(
        conversation=conv,
        sender=user,
        content=content[:2000]
    )

    # Touch updated_at timestamp to resort active chats
    conv.save(update_fields=['updated_at'])

    return {
        "status": "success",
        "message": {
            "id": message.id,
            "sender_email": user.email,
            "content": message.content,
            "created_at": message.created_at.isoformat()
        }
    }

@router.post("/chat/{conv_id}/end")
async def end_customer_chat(request, conv_id: str):
    user = request.auth

    @sync_to_async
    def execute_end():
        with transaction.atomic():
            try:
                conv = Conversation.objects.select_for_update().get(
                    id=conv_id, conversation_type='customer_support'
                )
            except Conversation.DoesNotExist:
                raise HttpError(404, "Conversation not found.")

            # Only the agent handling the chat (or an admin) may end it. Any agent used to be
            # able to close any customer's conversation, including ones assigned to others.
            if conv.assigned_staff_id != user.id and not (user.is_superuser or user.role == 'admin'):
                raise HttpError(403, "You are not assigned to this conversation.")

            conv.is_active = False
            conv.save(update_fields=['is_active', 'updated_at'])
            return conv.id

    conv_id_val = await execute_end()

    channel_layer = get_channel_layer()
    if channel_layer:
        # Broadcast closure event over WebSocket using native await
        await channel_layer.group_send(
            f"chat_{conv_id_val}",
            {
                "type": "chat_ended",
                "message": "This chat session has been ended by the support agent."
            }
        )

        # Also broadcast to queue to remove it from active lists
        await channel_layer.group_send(
            "support_queue",
            {
                "type": "queue_update_broadcast",
                "action": "chat_closed",
                "conversation_id": conv_id_val
            }
        )

    return {"status": "success", "message": "Conversation ended successfully."}

@router.get("/orders/lookup/{order_identifier}")
def agent_lookup_order(request, order_identifier: str):
    """
    Allows a support agent to look up an order by its ID, order reference, 
    marketplace order ID, or linked MarketplaceOrder UUID during support.
    """
    user = request.auth
    if user.role != 'agent' and not (user.is_superuser or user.role == 'admin'):
        raise HttpError(403, "Access restricted to Support Agents.")

    # 1. Build comprehensive query across logistics Order model fields
    query = Q(order_reference__iexact=order_identifier)
    
    if order_identifier.isdigit():
        query |= Q(id=int(order_identifier))
        query |= Q(marketplace_order_id=int(order_identifier))
    
    # Support UUID / string matching for marketplace orders or references
    query |= Q(marketplace_order__id__icontains=order_identifier)

    order = Order.objects.filter(query).select_related(
        'receiver', 
        'receiver__user',
        'assigned_rider', 
        'marketplace_order',
        'marketplace_order__vendor',
        'marketplace_order__vendor__user',
        'marketplace_order__customer'
    ).first()

    # Fallback: If not found in logistics dispatches, check MarketplaceOrder directly
    marketplace_order = None
    if not order and order_identifier.isdigit():
        marketplace_order = MarketplaceOrder.objects.filter(id=int(order_identifier)).select_related('vendor', 'vendor__user', 'customer', 'customer__user').first()
    elif not order:
        marketplace_order = MarketplaceOrder.objects.filter(Q(id__icontains=order_identifier)).select_related('vendor', 'vendor__user', 'customer', 'customer__user').first()

    if not order and not marketplace_order:
        raise HttpError(404, f"Order with identifier '{order_identifier}' not found.")

    # 2. Extract item details matching your high-performance summary structure
    serialized_items = []
    
    if order and hasattr(order, 'marketplace_order') and order.marketplace_order:
        items_cursor = MarketplaceOrderItem.objects.filter(order=order.marketplace_order).select_related(
            'product_item__product', 'product'
        )
        for item in items_cursor:
            p_name = "Unknown Item"
            if item.product_item and hasattr(item.product_item, 'product') and item.product_item.product:
                p_name = item.product_item.product.name
            elif item.product and hasattr(item.product, 'name'):
                p_name = item.product.name

            serialized_items.append({
                "product_name": str(p_name),
                "quantity": int(item.quantity),
                "price": float(item.price_at_order)
            })
    elif marketplace_order:
        items_cursor = MarketplaceOrderItem.objects.filter(order=marketplace_order).select_related(
            'product_item__product', 'product'
        )
        for item in items_cursor:
            p_name = "Unknown Item"
            if item.product_item and hasattr(item.product_item, 'product') and item.product_item.product:
                p_name = item.product_item.product.name
            elif item.product and hasattr(item.product, 'name'):
                p_name = item.product.name

            serialized_items.append({
                "product_name": str(p_name),
                "quantity": int(item.quantity),
                "price": float(item.price_at_order)
            })
    elif order and hasattr(order, 'items'):
        serialized_items = [
            {
                "product_name": getattr(item, 'product_name', str(item)),
                "quantity": int(item.quantity),
                "price": float(getattr(item, 'price', 0.0))
            } for item in order.items.all()
        ]

    # 3. Resolve unified customer name & financial data safely
    customer_name = "N/A"
    if order and hasattr(order, 'receiver') and order.receiver:
        customer_name = getattr(order.receiver, 'full_name', None) or getattr(order.receiver, 'email', 'N/A')
    elif marketplace_order and hasattr(marketplace_order, 'customer') and marketplace_order.customer:
        customer_name = getattr(marketplace_order.customer, 'full_name', None) or getattr(marketplace_order.customer, 'email', 'N/A')

    # 4. Resolve Sender / Vendor Details safely
    sender_name = "N/A"
    sender_phone = "N/A"
    sender_address = "N/A"

    target_vendor = None
    if order and hasattr(order, 'marketplace_order') and order.marketplace_order:
        target_vendor = order.marketplace_order.vendor

    elif marketplace_order:
        target_vendor = marketplace_order.vendor

    if target_vendor:
        sender_name = getattr(target_vendor, 'company_name', None) or getattr(target_vendor, 'name', 'N/A')
        sender_phone = getattr(target_vendor.user, 'phone_number', None) or getattr(target_vendor, 'phone_number', 'N/A')
        sender_address = getattr(target_vendor.user, 'street_address', None) or getattr(target_vendor, 'business_address', 'N/A')
    elif order and hasattr(order, 'sender'):
        # Fallback if your logistics Order model has a direct sender relation
        sender_name = getattr(order.sender, 'name', 'N/A')
        sender_phone = getattr(order.sender, 'phone', 'N/A')
        sender_address = getattr(order.sender, 'address', 'N/A')

    total_amount = 0.0
    payment_status = "N/A"
    delivery_address = "N/A"
    order_status = "PENDING"
    order_id_val = order.id if order else marketplace_order.id
    order_ref = getattr(order, 'order_reference', str(order_id_val)) if order else str(marketplace_order.id)
    
    if order:
        total_amount = float(getattr(order, 'total_amount', getattr(order, 'total', 0.0)))
        if hasattr(order, 'marketplace_order') and order.marketplace_order:
            total_amount = float(order.marketplace_order.total_price)
            payment_status = str(order.marketplace_order.status)
        payment_status = getattr(order, 'payment_status', payment_status)
        delivery_address = getattr(order, 'delivery_address', 'N/A')
        order_status = order.status
    elif marketplace_order:
        total_amount = float(marketplace_order.total_price)
        payment_status = str(marketplace_order.status)
        delivery_address = f"{marketplace_order.delivery_address}, {marketplace_order.base_location}"
        order_status = marketplace_order.status

    assigned_rider_name = "Unassigned"
    if order and hasattr(order, 'assigned_rider') and order.assigned_rider:
        assigned_rider_name = getattr(order.assigned_rider, 'full_name', str(order.assigned_rider))

    created_at_val = order.created_at.isoformat() if order and hasattr(order, 'created_at') else (marketplace_order.created_at.isoformat() if marketplace_order and hasattr(marketplace_order, 'created_at') else None)

    return {
        "status": "success",
        "order": {
            "id": order_id_val,
            "order_reference": order_ref,
            "marketplace_order_id": order.marketplace_order_id if (order and hasattr(order, 'marketplace_order_id')) else (marketplace_order.id if marketplace_order else None),
            "order_status": order_status,
            "payment_status": payment_status,
            "total_amount": total_amount,
            "customer_name": customer_name,
            "delivery_address": delivery_address,
            "sender_name": sender_name,
            "sender_phone": sender_phone,
            "sender_address": sender_address,
            "assigned_rider": assigned_rider_name,
            "created_at": created_at_val,
            "items": serialized_items
        }
    }