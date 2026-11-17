from ninja.errors import HttpError
from ninja.security import HttpBearer

class RequireRole(HttpBearer):
    """
    Custom Django Ninja authentication & authorization guard.
    Verifies JWT token and enforces minimum permission requirements (superuser, staff, etc.).
    """
    def __init__(self, allowed_roles=None, require_superuser=False, require_staff=False):
        super().__init__()
        self.allowed_roles = allowed_roles or []
        self.require_superuser = require_superuser
        self.require_staff = require_staff

    def authenticate(self, request, token):
        # Assumes your JWTAuth utility or token decoder attaches the user object to request.auth or request.user
        # Adjust this extraction based on how your existing JWTAuth works.
        from accounts.utils import verify_jwt_token # or your token validation logic
        user = verify_jwt_token(token)
        
        if not user or not user.is_active:
            raise HttpError(401, "Authentication credentials invalid or user inactive.")

        # Check Superuser requirement
        if self.require_superuser and not user.is_superuser:
            raise HttpError(403, "Administrator privilege required.")

        # Check Staff requirement
        if self.require_staff and not (user.is_staff or user.is_superuser):
            raise HttpError(403, "Management staff authorization required.")

        # Check Specific Role Lists (e.g., ['business', 'admin'])
        if self.allowed_roles and user.role not in self.allowed_roles and not user.is_superuser:
            raise HttpError(403, "Insufficient role permissions for this action.")

        # Attach user to request for endpoint access
        request.user = user
        return user