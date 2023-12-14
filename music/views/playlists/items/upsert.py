from django import forms
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import FormView

from activities.models import TimelineEvent
from ....models import PlaylistItem
from core.files import blurhash_image, resize_image
from core.models import Config
from music.models.playlist import Playlist
from users.views.base import IdentityViewMixin


class Upsert(IdentityViewMixin, FormView):
    template_name = "music/upsert_playlist_item.html"

    class form_class(forms.Form):
        name = forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "autofocus": "autofocus",
                    "placeholder": "Track name",
                },
            )
        )
        artist_name = forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "autofocus": "autofocus",
                    "placeholder": "Name of artist",
                },
            )
        )
        release_name = forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "autofocus": "autofocus",
                    "placeholder": "Name of release",
                },
            )
        )
        isrc = forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "autofocus": "autofocus",
                    "placeholder": "ISRC code",
                },
            )
        )

        def __init__(self, identity, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.identity = identity
            self.fields["text"].widget.attrs[
                "_"
            ] = rf"""
                init
                    -- Move cursor to the end of existing text
                    set my.selectionStart to my.value.length
                end

                on load or input
                -- Unicode-aware counting to match Python
                -- <LF> will be normalized as <CR><LF> in Django
                set characters to Array.from(my.value.replaceAll('\n','\r\n').trim()).length
                put {Config.system.playlist_item_length} - characters into #character-counter

                if characters > {Config.system.playlist_item_length} then
                    set #character-counter's style.color to 'var(--color-text-error)'
                    add [@disabled=] to #playlist_item-button
                else
                    set #character-counter's style.color to ''
                    remove @disabled from #playlist_item-button
                end
            """

        def clean_text(self):
            text = self.cleaned_data.get("text")
            # Check minimum interval
            last_playlist_item = self.identity.playlist_items.order_by("-created").first()
            if (
                last_playlist_item
                and (timezone.now() - last_playlist_item.created).total_seconds()
                < Config.system.playlist_item_minimum_interval
            ):
                raise forms.ValidationError(
                    f"You must wait at least {Config.system.playlist_item_minimum_interval} seconds between playlist_items"
                )
            if not text:
                return text
            # Check playlist_item length
            length = len(text)
            if length > Config.system.playlist_item_length:
                raise forms.ValidationError(
                    f"Maximum playlist_item length is {Config.system.playlist_item_length} characters (you have {length})"
                )
            return text

        def clean_image(self):
            value = self.cleaned_data.get("image")
            if value:
                max_mb = settings.SETUP.MEDIA_MAX_IMAGE_FILESIZE_MB
                max_bytes = max_mb * 1024 * 1024
                if value.size > max_bytes:
                    # Erase the file from our data to stop trying to show it again
                    self.files = {}
                    raise forms.ValidationError(
                        f"File must be {max_mb}MB or less (actual: {value.size / 1024 ** 2:.2f})"
                    )
            return value

    def get_form(self, form_class=None):
        return self.form_class(identity=self.identity, **self.get_form_kwargs())

    def get_initial(self):
        initial = super().get_initial()
        initial["visibility"] = self.identity.config_identity.default_playlist_item_visibility
        return initial

    def form_valid(self, form):
        # See if we need to make an image attachment
        attachments = []
        # Create the playlist_item
        playlist_item = PlaylistItem.create_local(
            author=self.identity,
            content=form.cleaned_data["text"],
            summary=form.cleaned_data.get("content_warning"),
            visibility=form.cleaned_data["visibility"],
            attachments=attachments,
        )
        # Add their own timeline event for immediate visibility
        TimelineEvent.add_playlist_item(self.identity, playlist_item)
        messages.success(self.request, "Your playlist_item was created.")
        return redirect(".")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["identity"] = self.identity
        context["section"] = "upsert_playlist_item"
        return context

