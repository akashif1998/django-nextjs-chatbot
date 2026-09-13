"""
Django admin configuration for CustomUser and UserContact models.
Provides comprehensive admin interface for user management.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.db import models
from django.forms import Textarea

from ..models.custom_user import CustomUser, UserContact


class UserContactInline(admin.StackedInline):
    """Inline admin for UserContact model."""

    model = UserContact
    extra = 0
    can_delete = False

    fieldsets = (
        (
            _("Address Information"),
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                    "timezone",
                )
            },
        ),
        (
            _("Contact Details"),
            {
                "fields": ("contact_info",),
                "classes": ("collapse",),
            },
        ),
        (
            _("Billing & Availability"),
            {
                "fields": ("billing_details", "availability"),
                "classes": ("collapse",),
            },
        ),
    )

    formfield_overrides = {
        models.JSONField: {"widget": Textarea(attrs={"rows": 4, "cols": 80})},
    }


@admin.register(CustomUser)
class CustomUserAdmin(BaseUserAdmin):
    """Admin configuration for CustomUser model."""

    inlines = [UserContactInline]

    # List display configuration
    list_display = (
        "email",
        "username",
        "get_full_name_display",
        "role",
        "email_verified",
        "is_active",
        "last_active",
        "date_joined",
    )

    list_display_links = ("email", "username")

    # List filters
    list_filter = (
        "role",
        "email_verified",
        "phone_verified",
        "two_factor_enabled",
        "is_active",
        "is_staff",
        "is_superuser",
        "date_joined",
        "last_active",
    )

    # Search fields
    search_fields = (
        "email",
        "username",
        "first_name",
        "last_name",
    )

    # Ordering
    ordering = ("-date_joined",)

    # Date hierarchy
    date_hierarchy = "date_joined"

    # Readonly fields
    readonly_fields = (
        "account_age_days",
        "last_password_change",
        "date_joined",
        "last_login",
        "login_count",
    )

    # Fieldset organization
    fieldsets = (
        (
            _("Basic Information"),
            {
                "fields": (
                    "email",
                    "username",
                    "first_name",
                    "last_name",
                    "password",
                )
            },
        ),
        (
            _("Role & Permissions"),
            {
                "fields": (
                    "role",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            _("Profile"),
            {
                "fields": ("profile_picture",),
            },
        ),
        (
            _("Verification & Security"),
            {
                "fields": (
                    "email_verified",
                    "phone_verified",
                    "two_factor_enabled",
                    "last_password_change",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Social Authentication"),
            {
                "fields": ("social_auth_providers",),
                "classes": ("collapse",),
            },
        ),
        (
            _("Activity & Engagement"),
            {
                "fields": (
                    "last_active",
                    "login_count",
                    "date_joined",
                    "last_login",
                    "account_age_days",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    # Add fieldsets for creating new users
    add_fieldsets = (
        (
            _("Essential Information"),
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "username",
                    "password1",
                    "password2",
                    "first_name",
                    "last_name",
                ),
            },
        ),
        (
            _("Role Assignment"),
            {
                "classes": ("wide",),
                "fields": ("role",),
            },
        ),
        (
            _("Permissions"),
            {
                "classes": ("wide",),
                "fields": (
                    "is_active",
                    "is_staff",
                ),
            },
        ),
    )

    # Custom form overrides for better JSON field display
    formfield_overrides = {
        models.JSONField: {"widget": Textarea(attrs={"rows": 4, "cols": 80})},
    }

    # Filter horizontal for many-to-many fields
    filter_horizontal = ("groups", "user_permissions")

    # Actions
    actions = [
        "activate_users",
        "deactivate_users",
        "verify_emails",
    ]

    def get_full_name_display(self, obj):
        """Display full name with fallback to username."""
        return obj.full_name

    get_full_name_display.short_description = _("Full Name")

    def account_age_days(self, obj):
        """Display account age in days."""
        return f"{obj.account_age_days} days"

    account_age_days.short_description = _("Account Age")

    # Custom admin actions
    def activate_users(self, request, queryset):
        """Activate selected users."""
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} user(s) were successfully activated.")

    activate_users.short_description = _("Activate selected users")

    def deactivate_users(self, request, queryset):
        """Deactivate selected users."""
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} user(s) were successfully deactivated.")

    deactivate_users.short_description = _("Deactivate selected users")

    def verify_emails(self, request, queryset):
        """Mark emails as verified."""
        updated = queryset.update(email_verified=True)
        self.message_user(
            request, f"{updated} user email(s) were successfully verified."
        )

    verify_emails.short_description = _("Verify user emails")


@admin.register(UserContact)
class UserContactAdmin(admin.ModelAdmin):
    """Admin configuration for UserContact model."""

    list_display = (
        "user",
        "get_user_email",
        "city",
        "state",
        "country",
        "timezone",
        "created_at",
    )

    list_filter = (
        "country",
        "timezone",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "user__email",
        "user__first_name",
        "user__last_name",
        "city",
        "state",
        "address_line1",
    )

    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (_("User"), {"fields": ("user",)}),
        (
            _("Address"),
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                    "timezone",
                )
            },
        ),
        (
            _("Contact Information"),
            {
                "fields": ("contact_info",),
                "classes": ("collapse",),
            },
        ),
        (
            _("Billing Details"),
            {
                "fields": ("billing_details",),
                "classes": ("collapse",),
            },
        ),
        (
            _("Availability"),
            {
                "fields": ("availability",),
                "classes": ("collapse",),
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    formfield_overrides = {
        models.JSONField: {"widget": Textarea(attrs={"rows": 4, "cols": 80})},
    }

    def get_user_email(self, obj):
        """Display user email."""
        return obj.user.email

    get_user_email.short_description = _("Email")
    get_user_email.admin_order_field = "user__email"
