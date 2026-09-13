# /home/ram/aparsoft/backend/apps/accounts/spectacular_extensions.py

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class CustomJWTCookieAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "accounts.services.auth.CustomJWTCookieAuthentication"
    name = "CustomJWTCookieAuth"  # Name that appears in schema

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",  # Since it's cookie-based JWT
            "name": "jwt",  # Replace with your actual cookie name
            "description": "JWT token authentication via cookie",
        }
