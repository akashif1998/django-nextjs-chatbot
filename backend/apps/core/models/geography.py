"""
Geography related models.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from .base import TimestampedModel


class Country(TimestampedModel):
    """Country model for geographical reference."""

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=3, unique=True)
    phone_code = models.CharField(max_length=5, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("country")
        verbose_name_plural = _("countries")
        ordering = ["name"]

    def __str__(self):
        return self.name
