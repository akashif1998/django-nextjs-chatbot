"""
Django admin configuration for accounts app.
Imports all admin classes to register them with Django admin.
"""

# Import all admin classes to register them
from .user_admin import CustomUserAdmin, UserContactAdmin

__all__ = [
    "CustomUserAdmin",
    "UserContactAdmin",
]
