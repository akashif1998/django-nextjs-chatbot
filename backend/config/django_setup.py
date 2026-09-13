import os
import django
from django.core.asgi import get_asgi_application
import logging

logger = logging.getLogger(__name__)

try:
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_module:
        # Fallback to development settings if not set
        settings_module = "config.settings.development"
        os.environ["DJANGO_SETTINGS_MODULE"] = settings_module

    logger.info(f"Using settings module: {settings_module}")
    # Set up Django
    django.setup()
    django_asgi_app = get_asgi_application()
except Exception as e:
    logger.error(f"Failed to initialize Django: {str(e)}")
    raise
