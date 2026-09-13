# /home/ram/aparsoft/backend/apps/accounts/models/__init__.py

"""
Core models package for the accounts app.
"""
from .custom_user import CustomUser, UserContact

__all__ = [
    "CustomUser",
    "UserContact",
]
