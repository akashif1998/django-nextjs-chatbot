# /home/ram/aparsoft/backend/config/settings/__init__.py

# config/settings.py
import os

# Default to development settings
environment = os.environ.get('DJANGO_SETTINGS_MODULE', 'development')

if environment == 'config.settings.production':
    from .production import *
    print(f"Imported all production settings")

elif environment == 'config.settings.test':
    from .test import *
    print(f"Imported all test settings")
else:
    from .development import *
    print(f"Imported all development settings")

print(f"Using {environment} settings")
