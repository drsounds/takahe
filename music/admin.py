from django.contrib import admin

from . import models


class PlaylistItemInline(admin.TabularInline):
    model = models.PlaylistItem


class PlaylistAdmin(admin.ModelAdmin):
    inlines = [PlaylistItemInline]


class PlaylistItemAdmin(admin.ModelAdmin):
    pass


admin.site.register(models.Playlist, PlaylistAdmin)
admin.site.register(models.PlaylistItem, PlaylistItemAdmin)