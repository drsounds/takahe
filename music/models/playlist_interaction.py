from collections.abc import Iterable

from django.db import models, transaction
from django.utils import timezone

from activities.models.fan_out import FanOut
from .playlist import Playlist
from .playlist_types import QuestionData
from core.ld import format_ld_date, get_str_or_id, parse_ld_date
from core.snowflake import Snowflake
from stator.models import State, StateField, StateGraph, StatorModel
from users.models.identity import Identity


class PlaylistInteractionStates(StateGraph):
    new = State(try_interval=300)
    fanned_out = State(externally_progressed=True)
    undone = State(try_interval=300)
    undone_fanned_out = State(delete_after=24 * 60 * 60)

    new.transitions_to(fanned_out)
    fanned_out.transitions_to(undone)
    undone.transitions_to(undone_fanned_out)

    @classmethod
    def group_active(cls):
        return [cls.new, cls.fanned_out]

    @classmethod
    def handle_new(cls, instance: "PlaylistInteraction"):
        """
        Creates all needed fan-out objects for a new PlaylistInteraction.
        """
        # Boost: send a copy to all people who follow this user (limiting
        # to just local follows if it's a remote boost)
        # Pin: send Add activity to all people who follow this user
        if instance.type == instance.Types.boost or instance.type == instance.Types.pin:
            for target in instance.get_targets():
                FanOut.objects.create(
                    type=FanOut.Types.interaction,
                    identity=target,
                    subject_playlist=instance.playlist,
                    subject_playlist_interaction=instance,
                )
        # Like: send a copy to the original playlist author only,
        # if the liker is local or they are
        elif instance.type == instance.Types.like:
            if instance.identity.local or instance.playlist.local:
                FanOut.objects.create(
                    type=FanOut.Types.interaction,
                    identity_id=instance.playlist.author_id,
                    subject_playlist=instance.playlist,
                    subject_playlist_interaction=instance,
                )
        # Vote: send a copy of the vote to the original
        # playlist author only if it's a local interaction
        # to a non local playlist
        elif instance.type == instance.Types.vote:
            if instance.identity.local and not instance.playlist.local:
                FanOut.objects.create(
                    type=FanOut.Types.interaction,
                    identity_id=instance.playlist.author_id,
                    subject_playlist=instance.playlist,
                    subject_playlist_interaction=instance,
                )
        else:
            raise ValueError("Cannot fan out unknown type")
        # And one for themselves if they're local and it's a boost
        if instance.type == PlaylistInteraction.Types.boost and instance.identity.local:
            FanOut.objects.create(
                identity_id=instance.identity_id,
                type=FanOut.Types.interaction,
                subject_playlist=instance.playlist,
                subject_playlist_interaction=instance,
            )
        return cls.fanned_out

    @classmethod
    def handle_undone(cls, instance: "PlaylistInteraction"):
        """
        Creates all needed fan-out objects to undo a PlaylistInteraction.
        """
        # Undo Boost: send a copy to all people who follow this user
        # Undo Pin: send a Remove activity to all people who follow this user
        if instance.type == instance.Types.boost or instance.type == instance.Types.pin:
            for follow in instance.identity.inbound_follows.select_related(
                "source", "target"
            ):
                if follow.source.local or follow.target.local:
                    FanOut.objects.create(
                        type=FanOut.Types.undo_interaction,
                        identity_id=follow.source_id,
                        subject_playlist=instance.playlist,
                        subject_playlist_interaction=instance,
                    )
        # Undo Like: send a copy to the original playlist author only
        elif instance.type == instance.Types.like:
            FanOut.objects.create(
                type=FanOut.Types.undo_interaction,
                identity_id=instance.playlist.author_id,
                subject_playlist=instance.playlist,
                subject_playlist_interaction=instance,
            )
        else:
            raise ValueError("Cannot fan out unknown type")
        # And one for themselves if they're local and it's a boost
        if instance.type == PlaylistInteraction.Types.boost and instance.identity.local:
            FanOut.objects.create(
                identity_id=instance.identity_id,
                type=FanOut.Types.undo_interaction,
                subject_playlist=instance.playlist,
                subject_playlist_interaction=instance,
            )
        return cls.undone_fanned_out


class PlaylistInteraction(StatorModel):
    """
    Handles both boosts and likes
    """

    class Types(models.TextChoices):
        like = "like"
        boost = "boost"
        vote = "vote"
        pin = "pin"

    id = models.BigIntegerField(
        primary_key=True,
        default=Snowflake.generate_post_interaction,
    )

    # The state the boost is in
    state = StateField(PlaylistInteractionStates)

    # The canonical object ID
    object_uri = models.CharField(max_length=500, blank=True, null=True, unique=True)

    # What type of interaction it is
    type = models.CharField(max_length=100, choices=Types.choices)

    # The user who boosted/liked/etc.
    identity = models.ForeignKey(
        "users.Identity",
        on_delete=models.CASCADE,
        related_name="playlist_identity_interactions",
    )

    # The playlist that was boosted/liked/etc
    playlist = models.ForeignKey(
        "music.Playlist",
        on_delete=models.CASCADE,
        related_name="playlist_interactions",
    )

    # Used to store any interaction extra text value like the vote
    # in the question/poll case
    value = models.CharField(max_length=50, blank=True, null=True)

    # When the activity was originally created (as opposed to when we received it)
    # Mastodon only seems to send this for boosts, not likes
    published = models.DateTimeField(default=timezone.now)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["type", "identity", "playlist"])]

    ### Display helpers ###

    @classmethod
    def get_playlist_interactions(cls, playlists, identity):
        """
        Returns a dict of {interaction_type: set(playlist_ids)} for all the playlists
        and the given identity, for use in templates.
        """
        # Bulk-fetch any of our own interactions
        ids_with_interaction_type = cls.objects.filter(
            identity=identity,
            playlist_id__in=[playlist.pk for playlist in playlists],
            type__in=[cls.Types.like, cls.Types.boost, cls.Types.pin],
            state__in=[PlaylistInteractionStates.new, PlaylistInteractionStates.fanned_out],
        ).values_list("playlist_id", "type")
        # Make it into the return dict
        result = {}
        for playlist_id, interaction_type in ids_with_interaction_type:
            result.setdefault(interaction_type, set()).add(playlist_id)
        return result

    @classmethod
    def get_event_interactions(cls, events, identity) -> dict[str, set[str]]:
        """
        Returns a dict of {interaction_type: set(playlist_ids)} for all the playlists
        within the events and the given identity, for use in templates.
        """
        return cls.get_playlist_interactions(
            [e.subject_playlist for e in events if e.subject_playlist], identity
        )

    def get_targets(self) -> Iterable[Identity]:
        """
        Returns an iterable with Identities of followers that have unique
        shared_inbox among each other to be used as target.

        When interaction is boost, only boost follows are considered,
        for pins all followers are considered.
        """
        # Start including the playlist author
        targets = {self.playlist.author}

        query = self.identity.inbound_follows.active()
        # Include all followers that are following the boosts
        if self.type == self.Types.boost:
            query = query.filter(boosts=True)
        for follow in query.select_related("source"):
            targets.add(follow.source)

        # Fetch the full blocks and remove them as targets
        for block in (
            self.identity.outbound_blocks.active()
            .filter(mute=False)
            .select_related("target")
        ):
            try:
                targets.remove(block.target)
            except KeyError:
                pass

        deduped_targets = set()
        shared_inboxes = set()
        for target in targets:
            if target.local:
                # Local targets always gets the boosts
                # despite its creator locality
                deduped_targets.add(target)
            elif self.identity.local:
                # Dedupe the targets based on shared inboxes
                # (we only keep one per shared inbox)
                if not target.shared_inbox_uri:
                    deduped_targets.add(target)
                elif target.shared_inbox_uri not in shared_inboxes:
                    shared_inboxes.add(target.shared_inbox_uri)
                    deduped_targets.add(target)

        return deduped_targets

    ### Create helpers ###

    @classmethod
    def create_votes(cls, playlist, identity, choices) -> list["PlaylistInteraction"]:
        question = playlist.type_data

        if question.end_time and timezone.now() > question.end_time:
            raise ValueError("Validation failed: The poll has already ended")

        if playlist.interactions.filter(identity=identity, type=cls.Types.vote).exists():
            raise ValueError("Validation failed: You have already voted on this poll")

        votes = []
        with transaction.atomic():
            for choice in set(choices):
                vote = cls.objects.create(
                    identity=identity,
                    playlist=playlist,
                    type=PlaylistInteraction.Types.vote,
                    value=question.options[choice].name,
                )
                vote.object_uri = f"{identity.actor_uri}#votes/{vote.id}"
                vote.save()
                votes.append(vote)

                if not playlist.local:
                    question.options[choice].votes += 1

            if not playlist.local:
                question.voter_count += 1

            playlist.calculate_type_data()

        return votes

    ### ActivityPub (outbound) ###

    def to_ap(self) -> dict:
        """
        Returns the AP JSON for this object
        """
        # Create an object URI if we don't have one
        if self.object_uri is None:
            self.object_uri = self.identity.actor_uri + f"#{self.type}/{self.id}"
        if self.type == self.Types.boost:
            value = {
                "type": "Announce",
                "id": self.object_uri,
                "published": format_ld_date(self.published),
                "actor": self.identity.actor_uri,
                "object": self.playlist.object_uri,
                "to": "as:Public",
            }
        elif self.type == self.Types.like:
            value = {
                "type": "Like",
                "id": self.object_uri,
                "published": format_ld_date(self.published),
                "actor": self.identity.actor_uri,
                "object": self.playlist.object_uri,
            }
        elif self.type == self.Types.vote:
            value = {
                "type": "Note",
                "id": self.object_uri,
                "to": self.playlist.author.actor_uri,
                "name": self.value,
                "inReplyTo": self.playlist.object_uri,
                "attributedTo": self.identity.actor_uri,
            }
        elif self.type == self.Types.pin:
            raise ValueError("Cannot turn into AP")
        return value

    def to_create_ap(self):
        """
        Returns the AP JSON to create this object
        """
        object = self.to_ap()
        return {
            "to": object.get("to", []),
            "cc": object.get("cc", []),
            "type": "Create",
            "id": self.object_uri,
            "actor": self.identity.actor_uri,
            "object": object,
        }

    def to_undo_ap(self) -> dict:
        """
        Returns the AP JSON to undo this object
        """
        object = self.to_ap()
        return {
            "id": object["id"] + "/undo",
            "type": "Undo",
            "actor": self.identity.actor_uri,
            "object": object,
        }

    def to_add_ap(self):
        """
        Returns the AP JSON to add a pin interaction to the featured collection
        """
        return {
            "type": "Add",
            "actor": self.identity.actor_uri,
            "object": self.playlist.object_uri,
            "target": self.identity.actor_uri + "collections/featured/",
        }

    def to_remove_ap(self):
        """
        Returns the AP JSON to remove a pin interaction from the featured collection
        """
        return {
            "type": "Remove",
            "actor": self.identity.actor_uri,
            "object": self.playlist.object_uri,
            "target": self.identity.actor_uri + "collections/featured/",
        }

    ### ActivityPub (inbound) ###

    @classmethod
    def by_ap(cls, data, create=False) -> "PlaylistInteraction":
        """
        Retrieves a PlaylistInteraction instance by its ActivityPub JSON object.

        Optionally creates one if it's not present.
        Raises KeyError if it's not found and create is False.
        """
        # Do we have one with the right ID?
        try:
            boost = cls.objects.get(object_uri=data["id"])
        except cls.DoesNotExist:
            if create:
                # Resolve the author
                identity = Identity.by_actor_uri(data["actor"], create=True)
                # Resolve the playlist
                object = data["object"]
                target = get_str_or_id(object, "inReplyTo") or get_str_or_id(object)
                playlist = Playlist.by_object_uri(target, fetch=True)
                value = None
                # Get the right type
                if data["type"].lower() == "like":
                    type = cls.Types.like
                elif data["type"].lower() == "announce":
                    type = cls.Types.boost
                elif (
                    data["type"].lower() == "create"
                    and object["type"].lower() == "note"
                    and isinstance(playlist.type_data, QuestionData)
                ):
                    type = cls.Types.vote
                    question = playlist.type_data
                    value = object["name"]
                    if question.end_time and timezone.now() > question.end_time:
                        # TODO: Maybe create an expecific expired exception?
                        raise cls.DoesNotExist(
                            f"Cannot create a vote to the expired question {playlist.id}"
                        )

                    already_voted = (
                        playlist.type_data.mode == "oneOf"
                        and playlist.interactions.filter(
                            type=cls.Types.vote, identity=identity
                        ).exists()
                    )
                    if already_voted:
                        raise cls.DoesNotExist(
                            f"The identity {identity.handle} already voted in question {playlist.id}"
                        )

                else:
                    raise ValueError(f"Cannot handle AP type {data['type']}")
                # Make the actual interaction
                boost = cls.objects.create(
                    object_uri=data["id"],
                    identity=identity,
                    playlist=playlist,
                    published=parse_ld_date(data.get("published", None))
                    or timezone.now(),
                    type=type,
                    value=value,
                )
            else:
                raise cls.DoesNotExist(f"No interaction with ID {data['id']}", data)
        return boost

    @classmethod
    def handle_ap(cls, data):
        """
        Handles an incoming announce/like
        """
        with transaction.atomic():
            # Create it
            try:
                interaction = cls.by_ap(data, create=True)
            except (cls.DoesNotExist, Playlist.DoesNotExist):
                # That playlist is gone, boss
                # TODO: Limited retry state?
                return

            if interaction and interaction.playlist:
                interaction.playlist.calculate_stats()
                interaction.playlist.calculate_type_data()

    @classmethod
    def handle_undo_ap(cls, data):
        """
        Handles an incoming undo for a announce/like
        """
        with transaction.atomic():
            # Find it
            try:
                interaction = cls.by_ap(data["object"])
            except (cls.DoesNotExist, Playlist.DoesNotExist):
                # Well I guess we don't need to undo it do we
                return
            # Verify the actor matches
            if data["actor"] != interaction.identity.actor_uri:
                raise ValueError("Actor mismatch on interaction undo")
            # Delete all events that reference it
            interaction.timeline_events.all().delete()
            # Force it into undone_fanned_out as it's not ours
            interaction.transition_perform(PlaylistInteractionStates.undone_fanned_out)
            # Recalculate playlist stats
            interaction.playlist.calculate_stats()
            interaction.playlist.calculate_type_data()

    @classmethod
    def handle_add_ap(cls, data):
        """
        Handles an incoming Add activity which is a pin
        """
        target = data.get("target", None)
        if not target:
            return

        # we only care about pinned playlists, not hashtags
        object = data.get("object", {})
        if isinstance(object, dict) and object.get("type") == "Hashtag":
            return

        with transaction.atomic():
            identity = Identity.by_actor_uri(data["actor"], create=True)
            # it's only a pin if the target is the identity's featured collection URI
            if identity.featured_collection_uri != target:
                return

            object_uri = get_str_or_id(object)
            if not object_uri:
                return
            playlist = Playlist.by_object_uri(object_uri, fetch=True)

            return PlaylistInteraction.objects.get_or_create(
                type=cls.Types.pin,
                identity=identity,
                playlist=playlist,
                state__in=PlaylistInteractionStates.group_active(),
            )[0]

    @classmethod
    def handle_remove_ap(cls, data):
        """
        Handles an incoming Remove activity which is an unpin
        """
        target = data.get("target", None)
        if not target:
            return

        # we only care about pinned playlists, not hashtags
        object = data.get("object", {})
        if isinstance(object, dict) and object.get("type") == "Hashtag":
            return

        with transaction.atomic():
            identity = Identity.by_actor_uri(data["actor"], create=True)
            # it's only an unpin if the target is the identity's featured collection URI
            if identity.featured_collection_uri != target:
                return

            try:
                object_uri = get_str_or_id(object)
                if not object_uri:
                    return
                playlist = Playlist.by_object_uri(object_uri, fetch=False)
                for interaction in cls.objects.filter(
                    type=cls.Types.pin,
                    identity=identity,
                    playlist=playlist,
                    state__in=PlaylistInteractionStates.group_active(),
                ):
                    # Force it into undone_fanned_out as it's not ours
                    interaction.transition_perform(
                        PlaylistInteractionStates.undone_fanned_out
                    )
            except (cls.DoesNotExist, Playlist.DoesNotExist):
                return

    ### Mastodon API ###

    def to_mastodon_status_json(self, interactions=None, identity=None):
        """
        This wraps Playlists in a fake Status for boost interactions.
        """
        if self.type != self.Types.boost:
            raise ValueError(
                f"Cannot make status JSON for interaction of type {self.type}"
            )
        # Make a fake playlist for this boost (because mastodon treats boosts as playlists)
        playlist_json = self.playlist.to_mastodon_json(
            interactions=interactions, identity=identity
        )
        return {
            "id": f"{self.pk}",
            "uri": playlist_json["uri"],
            "created_at": format_ld_date(self.published),
            "account": self.identity.to_mastodon_json(include_counts=False),
            "content": "",
            "visibility": playlist_json["visibility"],
            "sensitive": playlist_json["sensitive"],
            "spoiler_text": playlist_json["spoiler_text"],
            "media_attachments": [],
            "mentions": [],
            "tags": [],
            "emojis": [],
            "reblogs_count": 0,
            "favourites_count": 0,
            "replies_count": 0,
            "url": playlist_json["url"],
            "in_reply_to_id": None,
            "in_reply_to_account_id": None,
            "poll": playlist_json["poll"],
            "card": None,
            "language": None,
            "text": "",
            "edited_at": None,
            "reblog": playlist_json,
        }
