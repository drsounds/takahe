import re
from datetime import date, timedelta

import urlman
from django.db import models
from django.utils import timezone

from core.models import Config
from stator.models import State, StateField, StateGraph, StatorModel


class PlaylistStates(StateGraph):
    outdated = State(try_interval=300, force_initial=True)
    updated = State(externally_progressed=True)

    outdated.transitions_to(updated)
    updated.transitions_to(outdated)

    @classmethod
    def handle_outdated(cls, instance: "Playlist"):
        """
        Computes the stats and other things for a Playlist
        """
        from activities.models.post import Post

        posts_query = Post.objects.local_public().playlistged_with(instance)
        total = posts_query.count()

        today = timezone.now().date()
        total_today = posts_query.filter(
            created__gte=today,
            created__lte=today + timedelta(days=1),
        ).count()
        total_month = posts_query.filter(
            created__year=today.year,
            created__month=today.month,
        ).count()
        total_year = posts_query.filter(
            created__year=today.year,
        ).count()
        if total:
            if not instance.stats:
                instance.stats = {}
            instance.stats.update(
                {
                    "total": total,
                    today.isoformat(): total_today,
                    today.strftime("%Y-%m"): total_month,
                    today.strftime("%Y"): total_year,
                }
            )
            instance.stats_updated = timezone.now()
            instance.save()

        return cls.updated


class PlaylistQuerySet(models.QuerySet):
    def public(self):
        public_q = models.Q(public=True)
        if Config.system.playlist_unreviewed_are_public:
            public_q |= models.Q(public__isnull=True)
        return self.filter(public_q)

    def playlist_or_alias(self, playlist: str):
        return self.filter(
            models.Q(playlist=playlist) | models.Q(aliases__contains=playlist)
        )


class PlaylistManager(models.Manager):
    def get_queryset(self):
        return PlaylistQuerySet(self.model, using=self._db)

    def public(self):
        return self.get_queryset().public()

    def playlist_or_alias(self, playlist: str):
        return self.get_queryset().playlist_or_alias(playlist)


class Playlist(StatorModel):
    MAXIMUM_LENGTH = 100

    # Normalized playlist without the '#'
    playlist = models.SlugField(primary_key=True, max_length=100)

    # Friendly display override
    name_override = models.CharField(max_length=100, null=True, blank=True)

    # Should this be shown in the public UI?
    public = models.BooleanField(null=True)

    name = models.TextField(null=True, blank=True)

    # State of this Playlist
    state = StateField(PlaylistStates)

    # Metrics for this Playlist
    stats = models.JSONField(null=True, blank=True)
    # Timestamp of last time the stats were updated
    stats_updated = models.DateTimeField(null=True, blank=True)

    # List of other playlists that are considered similar
    aliases = models.JSONField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = PlaylistManager()

    class urls(urlman.Urls):
        view = "/playlists/{self.playlist}/"
        follow = "/playlists/{self.playlist}/follow/"
        unfollow = "/playlists/{self.playlist}/unfollow/"
        admin = "/admin/playlists/"
        admin_edit = "{admin}{self.playlist}/"
        admin_enable = "{admin_edit}enable/"
        admin_disable = "{admin_edit}disable/"
        timeline = "/playlists/{self.playlist}/"

    playlist_regex = re.compile(r"\B#([a-zA-Z0-9(_)]+\b)(?!;)")

    def save(self, *args, **kwargs):
        self.playlist = self.playlist.lstrip("#")
        if self.name_override:
            self.name_override = self.name_override.lstrip("#")
        return super().save(*args, **kwargs)

    @property
    def display_name(self):
        return self.name_override or self.playlist

    def __str__(self):
        return self.display_name

    def usage_months(self, num: int = 12) -> dict[date, int]:
        """
        Return the most recent num months of stats
        """
        if not self.stats:
            return {}
        results = {}
        for key, val in self.stats.items():
            parts = key.split("-")
            if len(parts) == 2:
                year = int(parts[0])
                month = int(parts[1])
                results[date(year, month, 1)] = val
        return dict(sorted(results.items(), reverse=True)[:num])

    def usage_days(self, num: int = 7) -> dict[date, int]:
        """
        Return the most recent num days of stats
        """
        if not self.stats:
            return {}
        results = {}
        for key, val in self.stats.items():
            parts = key.split("-")
            if len(parts) == 3:
                year = int(parts[0])
                month = int(parts[1])
                day = int(parts[2])
                results[date(year, month, day)] = val
        return dict(sorted(results.items(), reverse=True)[:num])
    
    @property
    def delta(self):
        return self.get_delta()
 
    def get_delta(self, datum=None):
        """
        Gets delta of tracklist (additions minus deletions)
        """
        items = []
        if not datum:
            datum = timezone.now()
        for playlist_item in self.items.filter(
            created__lte=datum
        ):
            if playlist_item.operation == 'add':
                items.append(playlist_item)
            
            if playlist_item.operation == 'delete':
                items = [
                    item
                    for 
                    item
                    in
                    items
                    if
                    item.isrc != playlist_item.isrc or (
                        item.name != playlist_item.name and
                        item.artist_name != playlist_item.artist_name and
                        item.release_name != playlist_item.release_name and
                        item.upc != playlist_item.upc and
                        item.isni != playlist_item.isni
                    )
                ]
        return items           

    def to_mastodon_json(self, following: bool | None = None):
        value = {
            "name": self.playlist,
            "url": self.urls.view.full(),  # type: ignore
            "history": [],
        }

        if following is not None:
            value["following"] = following

        return value
