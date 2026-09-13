# /home/ram/aparsoft/backend/apps/accounts/utils/oauth.py

# backend/apps/accounts/utils/oauth.py

import requests
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def get_oauth_user_info(provider, code, redirect_uri):
    """
    Get user info from OAuth provider using authorization code.

    Args:
        provider (str): OAuth provider name (google, github, etc.)
        code (str): Authorization code from OAuth provider
        redirect_uri (str): Redirect URI used in the OAuth flow

    Returns:
        dict: User info or None if authentication failed
    """
    if provider.lower() == 'google':
        return get_google_user_info(code, redirect_uri)
    elif provider.lower() == 'github':
        return get_github_user_info(code, redirect_uri)
    else:
        logger.error(f"Unsupported OAuth provider: {provider}")
        return None


def get_google_user_info(code, redirect_uri):
    """Get user info from Google OAuth"""
    # Step 1: Exchange code for access token
    token_url = 'https://oauth2.googleapis.com/token'
    token_data = {
        'code': code,
        'client_id': settings.OAUTH['GOOGLE']['CLIENT_ID'],
        'client_secret': settings.OAUTH['GOOGLE']['CLIENT_SECRET'],
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code'
    }

    try:
        token_response = requests.post(token_url, data=token_data)
        token_response.raise_for_status()
        tokens = token_response.json()

        # Step 2: Get user info using access token
        user_info_url = 'https://www.googleapis.com/oauth2/v3/userinfo'
        headers = {'Authorization': f"Bearer {tokens['access_token']}"}
        user_info_response = requests.get(user_info_url, headers=headers)
        user_info_response.raise_for_status()
        user_info = user_info_response.json()

        # Format user info
        return {
            'id': user_info['sub'],
            'email': user_info['email'],
            'first_name': user_info.get('given_name', ''),
            'last_name': user_info.get('family_name', ''),
            'picture': user_info.get('picture', None),
            'provider': 'google'
        }
    except Exception as e:
        logger.error(f"Error getting Google user info: {str(e)}")
        return None


def get_github_user_info(code, redirect_uri):
    """Get user info from GitHub OAuth"""
    # Step 1: Exchange code for access token
    token_url = 'https://github.com/login/oauth/access_token'
    token_data = {
        'code': code,
        'client_id': settings.OAUTH['GITHUB']['CLIENT_ID'],
        'client_secret': settings.OAUTH['GITHUB']['CLIENT_SECRET'],
        'redirect_uri': redirect_uri
    }
    headers = {'Accept': 'application/json'}

    try:
        token_response = requests.post(
            token_url, data=token_data, headers=headers)
        token_response.raise_for_status()
        tokens = token_response.json()

        # Step 2: Get user info using access token
        user_info_url = 'https://api.github.com/user'
        headers = {'Authorization': f"token {tokens['access_token']}"}
        user_info_response = requests.get(user_info_url, headers=headers)
        user_info_response.raise_for_status()
        user_info = user_info_response.json()

        # Step 3: Get user email (GitHub doesn't include it by default)
        email_url = 'https://api.github.com/user/emails'
        email_response = requests.get(email_url, headers=headers)
        email_response.raise_for_status()
        emails = email_response.json()

        # Get primary email
        primary_email = next((email['email']
                             for email in emails if email['primary']), None)

        # Format user info
        name_parts = user_info.get('name', '').split(
            ' ', 1) if user_info.get('name') else ['', '']
        return {
            'id': str(user_info['id']),
            'email': primary_email or f"{user_info['login']}@github.com",
            'first_name': name_parts[0],
            'last_name': name_parts[1] if len(name_parts) > 1 else '',
            'username': user_info['login'],
            'picture': user_info.get('avatar_url', None),
            'provider': 'github'
        }
    except Exception as e:
        logger.error(f"Error getting GitHub user info: {str(e)}")
        return None
