from django.contrib import admin

from .models import Video


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "status", "progress", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("original_name",)
    readonly_fields = ("created_at", "updated_at")
