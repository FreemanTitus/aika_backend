
from decimal import Decimal
from ninja import Router
from ninja.errors import HttpError
from asgiref.sync import sync_to_async
from accounts.utils import JWTAuth
from order.models import UserCartItem
from .schema import AddCartItemInput, CartQuantityUpdateInput


router = Router(
    auth=JWTAuth(),
)

@router.post("/add", auth=JWTAuth())
async def add_customer_cart_item_api(request, data: AddCartItemInput):
    """
    Adds a single product to the authenticated user's cart or 
    increments its quantity if it already exists in the database.
    """
    user = request.user

    if data.quantity <= 0:
        # If quantity is 0 or less, delete the item if it exists
        await sync_to_async(UserCartItem.objects.filter(user=user, product_id=data.product_id).delete)()
        return {"status": "success", "message": "Item removed from cart."}

    # Check if item already exists in the cart to handle quantity increments smoothly
    def _get_or_create_cart_item():
        cart_item, created = UserCartItem.objects.get_or_create(
            user=user,
            product_id=data.product_id,
            defaults={
                'quantity': data.quantity,
                'store_id': data.store_id,
                'name': data.name,
                'price': Decimal(str(data.price or 0)),
                'vendor_name': data.vendor_name
            }
        )
        if not created:
            # Increment quantity if item is already in cart
            cart_item.quantity += data.quantity
            if data.price:
                cart_item.price = Decimal(str(data.price))
            if data.name:
                cart_item.name = data.name
            if data.store_id:
                cart_item.store_id = data.store_id
            if data.vendor_name:
                cart_item.vendor_name = data.vendor_name
            cart_item.save()
        return cart_item

    await sync_to_async(_get_or_create_cart_item)()

    return {"status": "success", "message": "Item added to cart successfully."}

@router.delete("/remove/{product_id}", auth=JWTAuth())
async def remove_customer_cart_item_api(request, product_id: str):
    """
    Removes a specific product from the authenticated user's database cart.
    """
    user = request.user

    # Delete the specific cart item safely using sync_to_async
    deleted_count, _ = await sync_to_async(
        UserCartItem.objects.filter(user=user, product_id=product_id).delete
    )()

    if deleted_count == 0:
        raise HttpError(404, "Cart item not found.")

    return {
        "status": "success",
        "message": "Cart item removed successfully."
    }

@router.post("/update/{product_id}", auth=JWTAuth())
async def update_cart_item_quantity_api(request, product_id: str, data: CartQuantityUpdateInput):
    """
    Updates a cart item's quantity (increment, decrement, or set explicitly)
    for the authenticated user. Removes the item if quantity reaches 0.
    """
    user = request.user

    def _update_qty():
        try:
            cart_item = UserCartItem.objects.get(user=user, product_id=product_id)
        except UserCartItem.DoesNotExist:
            raise HttpError(404, "Cart item not found.")

        if data.quantity is not None:
            cart_item.quantity = data.quantity
        elif data.action == 'increment':
            cart_item.quantity += 1
        elif data.action == 'decrement':
            cart_item.quantity -= 1
        
        if cart_item.quantity <= 0:
            cart_item.delete()
            return {"status": "success", "message": "Item removed from cart.", "quantity": 0}
        else:
            cart_item.save()
            return {"status": "success", "message": "Cart updated successfully.", "quantity": cart_item.quantity}

    return await sync_to_async(_update_qty)()

