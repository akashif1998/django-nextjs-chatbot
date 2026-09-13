# /home/ram/aparsoft/backend/apps/accounts/signals/user_creation_signals.py

"""
User Creation and Profile Management Signals Module

This module manages automated profile creation for the chatbot application.

Key functionalities:
- Automatic contact information creation for new users
- User activity tracking

Signal Flow:
1. New User Created → Creates contact information with default values
"""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction, IntegrityError

from ..models import CustomUser, UserContact
from core.models import Country

import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=CustomUser)
def create_user_contact(sender, instance, created, **kwargs):
    """
    Signal to create contact information for new users.
    """
    if created:
        try:
            with transaction.atomic():
                # Create contact info for all user types
                try:
                    # Try to get a default country (create one if none exists)
                    default_country = None
                    try:
                        # Try to get any active country, preferably India or USA
                        default_country = Country.objects.filter(is_active=True).first()
                        if not default_country:
                            # Create a default country if none exists
                            default_country = Country.objects.create(
                                name="United States",
                                code="US",
                                phone_code="+1",
                                is_active=True,
                            )
                            logger.info("Created default country (United States)")
                    except Exception as e:
                        logger.warning(
                            f"Could not create/get default country: {str(e)}"
                        )
                        default_country = None

                    UserContact.objects.create(
                        user=instance,
                        contact_info={},  # Will use default from helper
                        country=default_country,
                    )
                    logger.info(f"Created user contact for {instance.email}")
                except IntegrityError:
                    logger.warning(
                        f"Contact information already exists for user {instance.email}. Skipping creation."
                    )
                except Exception as e:
                    logger.error(
                        f"Error creating user contact for {instance.email}: {str(e)}"
                    )

                logger.info(
                    f"Created profile for user {instance.email} with role {instance.role}"
                )

        except IntegrityError as e:
            # Handle specific database integrity errors
            if "violates foreign key constraint" in str(e):
                logger.error(
                    f"Foreign key constraint error for user {instance.email}: {str(e)}"
                )
            else:
                logger.error(
                    f"Database integrity error creating profile for user {instance.email}: {str(e)}"
                )
        except Exception as e:
            logger.error(
                f"Error creating profile for user {instance.email}: {str(e)}",
                exc_info=True,
            )
