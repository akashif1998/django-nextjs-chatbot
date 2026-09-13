from django.contrib import admin
from ..models import Country


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "phone_code", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "code")
    ordering = ("name",)
    list_per_page = 50
