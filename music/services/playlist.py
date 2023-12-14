import logging

from music.models import (
    Playlist,
    PlaylistInteraction,
    PlaylistInteractionStates,
    PlaylistStates
)
from activities.models import TimelineEvent
from music.models.playlist_item import PlaylistItem, PlaylistItemStates

from users.models import Identity

logger = logging.getLogger(__name__)


class PlaylistService:
    """
    High-level operations on Playlists
    """

    @classmethod
    def queryset(cls):
        """
        Returns the base queryset to use for fetching playlists efficiently.
        """
        return (
            Playlist.objects.not_hidden()
            .prefetch_related(
                "attachments",
                "mentions",
                "emojis",
            )
            .select_related(
                "author",
                "author__domain",
            )
        )

    def __init__(self, playlist: Playlist):
        self.playlist = playlist

    def upsert_item_to_playlist(
        self,
        identity: Identity,
        type: str,
        name: str = "",
        artist_name: str = "",
        release_name: str = "",
        number: str = 0,
        isrc: str = None,
        upc: str = None,
        isni: str = None,
        operation: str = None
    ):
        playlist_item = None
        if isrc is not None:
            playlist_item, created = PlaylistItem.objects.get_or_create(
                isrc=isrc,
                type=type,
                identity=identity,
                playlist=self.playlist,
                operation=operation,
                defaults=dict(
                    number=number,
                    name=name,
                    artist_name=artist_name,
                    release_name=release_name,
                    upc=upc,
                    isni=isni
                )
            )
        else:
            playlist_item = PlaylistItem.objects.create(
                type=type,
                identity=identity,
                playlist=self.playlist,
                number=number,
                name=name,
                artist_name=artist_name,
                release_name=release_name,
                upc=upc,
                isni=isni,
                isrc=isrc,
                operation=operation
            )  

        if playlist_item.state not in PlaylistItemStates.group_active():
            playlist_item.transition_perform(PlaylistItemStates.new)
        self.playlist.calculate_stats()

    def interact_as(self, identity: Identity, type: str):
        """
        Performs an interaction on this Playlist
        """
        interaction = PlaylistInteraction.objects.get_or_create(
            type=type,
            identity=identity,
            playlist=self.playlist,
        )[0]
        if interaction.state not in PlaylistInteractionStates.group_active():
            interaction.transition_perform(PlaylistInteractionStates.new)
        self.playlist.calculate_stats()

    def uninteract_as(self, identity, type):
        """
        Undoes an interaction on this Playlist
        """
        for interaction in PlaylistInteraction.objects.filter(
            type=type,
            identity=identity,
            playlist=self.playlist,
        ):
            interaction.transition_perform(PlaylistInteractionStates.undone)
        self.playlist.calculate_stats()

    def like_as(self, identity: Identity):
        self.interact_as(identity, PlaylistInteraction.Types.like)

    def unlike_as(self, identity: Identity):
        self.uninteract_as(identity, PlaylistInteraction.Types.like)

    def boost_as(self, identity: Identity):
        self.interact_as(identity, PlaylistInteraction.Types.boost)

    def unboost_as(self, identity: Identity):
        self.uninteract_as(identity, PlaylistInteraction.Types.boost)

    def context(
        self,
        identity: Identity | None,
        num_ancestors: int = 10,
        num_descendants: int = 50,
    ) -> tuple[list[Playlist], list[Playlist]]:
        """
        Returns ancestor/descendant information.

        Ancestors are guaranteed to be in order from closest to furthest.
        Descendants are in depth-first order, starting with closest.

        If identity is provided, includes mentions/followers-only playlists they
        can see. Otherwise, shows unlisted and above only.
        """
        # Retrieve ancestors via parent walk
        ancestors: list[Playlist] = []
        ancestor = self.playlist
        while ancestor.in_reply_to and len(ancestors) < num_ancestors:
            object_uri = ancestor.in_reply_to
            reason = ancestor.object_uri
            ancestor = self.queryset().filter(object_uri=object_uri).first()
            if ancestor is None:
                try:
                    Playlist.ensure_object_uri(object_uri, reason=reason)
                except ValueError:
                    logger.error(
                        f"Cannot fetch ancestor Playlist={self.playlist.pk}, ancestor_uri={object_uri}"
                    )
                break
            if ancestor.state in [PlaylistStates.deleted, PlaylistStates.deleted_fanned_out]:
                break
            ancestors.append(ancestor)
        # Retrieve descendants via breadth-first-search
        descendants: list[Playlist] = []
        queue = [self.playlist]
        seen: set[str] = set()
        while queue and len(descendants) < num_descendants:
            node = queue.pop()
            child_queryset = (
                self.queryset()
                .filter(in_reply_to=node.object_uri)
                .order_by("published")
            )
            if identity:
                child_queryset = child_queryset.visible_to(
                    identity=identity, include_replies=True
                )
            else:
                child_queryset = child_queryset.unlisted(include_replies=True)
            for child in child_queryset:
                if child.pk not in seen:
                    descendants.append(child)
                    queue.append(child)
                    seen.add(child.pk)
        return ancestors, descendants

    def delete(self):
        """
        Marks a playlist as deleted and immediately cleans up its timeline events etc.
        """
        self.playlist.transition_perform(PlaylistStates.deleted)
        TimelineEvent.objects.filter(subject_playlist=self.playlist).delete()
        PlaylistInteraction.transition_perform_queryset(
            PlaylistInteraction.objects.filter(
                playlist=self.playlist,
                state__in=PlaylistInteractionStates.group_active(),
            ),
            PlaylistInteractionStates.undone,
        )

    def pin_as(self, identity: Identity):
        if identity != self.playlist.author:
            raise ValueError("Not the author of this playlist")
        if self.playlist.visibility == Playlist.Visibilities.mentioned:
            raise ValueError("Cannot pin a mentioned-only playlist")
        if (
            PlaylistInteraction.objects.filter(
                type=PlaylistInteraction.Types.pin,
                identity=identity,
            ).count()
            >= 5
        ):
            raise ValueError("Maximum number of pins already reached")

        self.interact_as(identity, PlaylistInteraction.Types.pin)

    def unpin_as(self, identity: Identity):
        self.uninteract_as(identity, PlaylistInteraction.Types.pin)
