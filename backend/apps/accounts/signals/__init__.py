# /home/ram/aparsoft/backend/apps/accounts/signals/__init__.py

"""
Signals package for the accounts app.

This package contains signal handlers for user management and profile creation.
All signals are automatically imported when the accounts app is ready.
"""

from .user_creation_signals import *

__all__ = [
    'create_user_profile',
    'handle_role_change',
    'handle_developer_status_change',
    'handle_client_status_change',
    'handle_project_manager_status_change',
    'handle_account_manager_status_change',
    'update_organization_user_counts',
    'decrease_organization_user_counts',
]