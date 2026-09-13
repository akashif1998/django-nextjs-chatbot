# /home/ram/aparsoft/backend/apps/accounts/utils/profile_picture_utils.py

"""
Utility functions for handling profile picture operations with graceful fallbacks.
"""

import logging
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


def has_profile_picture_field(user):
    """
    Check if the user model has a profile_picture field.
    
    Args:
        user: User instance
        
    Returns:
        bool: True if profile_picture field exists
    """
    return hasattr(user, 'profile_picture')


def get_profile_picture_url(user, request=None):
    """
    Safely get profile picture URL for a user.
    
    Args:
        user: User instance
        request: Request object for building absolute URI
        
    Returns:
        str or None: Profile picture URL or None if not available
    """
    try:
        if not has_profile_picture_field(user):
            logger.warning(f"Profile picture field not found for user {user.id}. Run migrations.")
            return None
            
        profile_picture = getattr(user, 'profile_picture', None)
        
        if not profile_picture:
            return None
            
        # Check if file exists
        if not default_storage.exists(profile_picture.name):
            logger.warning(f"Profile picture file missing for user {user.id}: {profile_picture.name}")
            # Clear the invalid reference
            user.profile_picture = None
            user.save(update_fields=['profile_picture'])
            return None
            
        # Generate URL
        if request:
            return request.build_absolute_uri(default_storage.url(profile_picture.name))
        else:
            return default_storage.url(profile_picture.name)
            
    except Exception as e:
        logger.error(f"Error getting profile picture URL for user {user.id}: {str(e)}")
        return None


def set_profile_picture(user, file_path):
    """
    Safely set profile picture for a user.
    
    Args:
        user: User instance
        file_path: Path to the new profile picture file
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if not has_profile_picture_field(user):
            logger.error(f"Cannot set profile picture - field not found for user {user.id}")
            return False
            
        # Delete old profile picture if exists
        old_picture = getattr(user, 'profile_picture', None)
        if old_picture:
            try:
                if default_storage.exists(old_picture.name):
                    default_storage.delete(old_picture.name)
            except Exception as e:
                logger.warning(f"Failed to delete old profile picture for user {user.id}: {str(e)}")
        
        # Set new profile picture
        user.profile_picture = file_path
        user.save(update_fields=['profile_picture'])
        
        logger.info(f"Profile picture updated for user {user.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error setting profile picture for user {user.id}: {str(e)}")
        return False


def delete_profile_picture(user):
    """
    Safely delete profile picture for a user.
    
    Args:
        user: User instance
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        if not has_profile_picture_field(user):
            logger.error(f"Cannot delete profile picture - field not found for user {user.id}")
            return False
            
        profile_picture = getattr(user, 'profile_picture', None)
        if not profile_picture:
            return True  # Already no profile picture
            
        # Delete file from storage
        try:
            if default_storage.exists(profile_picture.name):
                default_storage.delete(profile_picture.name)
        except Exception as e:
            logger.warning(f"Failed to delete profile picture file for user {user.id}: {str(e)}")
        
        # Clear from user model
        user.profile_picture = None
        user.save(update_fields=['profile_picture'])
        
        logger.info(f"Profile picture deleted for user {user.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting profile picture for user {user.id}: {str(e)}")
        return False


def get_user_profile_data(user, request=None):
    """
    Get comprehensive profile data for a user including profile picture.
    
    Args:
        user: User instance
        request: Request object for building absolute URIs
        
    Returns:
        dict: Profile data dictionary
    """
    profile_data = {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'full_name': user.get_full_name(),
        'role': user.role,
        'profile_picture_url': get_profile_picture_url(user, request),
        'has_profile_picture': bool(get_profile_picture_url(user, request)),
        'profile_picture_available': has_profile_picture_field(user),
    }
    
    return profile_data
