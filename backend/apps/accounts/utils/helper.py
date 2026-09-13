# /home/ram/aparsoft/backend/apps/accounts/utils/helper.py

"""
Helper functions for the accounts app.
"""

def get_default_social_auth_providers():
    """Default structure for social authentication providers."""
    return {
        'active_providers': [],
        'connections': {},
        'default_login': None
    }


def get_default_user_contact_info():
    """Default structure for user contact information."""
    return {
        'phone': {
            'primary': None,
            'secondary': None,
            'verified': False
        },
        'social': {
            'linkedin': None,
            'twitter': None,
            'github': None,
            'custom': []
        },
        'emergency_contact': {
            'name': None,
            'relationship': None,
            'phone': None
        }
    }
