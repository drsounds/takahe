from django import forms
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import FormView

from activities.models import TimelineEvent

from ...models import Playlist, PlaylistAttachment, PlaylistAttachmentStates
from core.files import blurhash_image, resize_image
from core.models import Config
from users.views.base import IdentityViewMixin


class Create(IdentityViewMixin, FormView):
    template_name = "music/playlists/upsert.html"

    class form_class(forms.Form):
        name = forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "autofocus": "autofocus",
                    "placeholder": "What's on your mind?",
                },
            )
        )

        description = forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "autofocus": "autofocus",
                    "placeholder": "Enter a description",
                },
            )
        )

        visibility = forms.ChoiceField(
            choices=[
                (Playlist.Visibilities.public, "Public"),
                (Playlist.Visibilities.local_only, "Local Only"),
                (Playlist.Visibilities.unlisted, "Unlisted"),
                (Playlist.Visibilities.followers, "Followers & Mentioned Only"),
                (Playlist.Visibilities.mentioned, "Mentioned Only"),
            ],
        )

        content_warning = forms.CharField(
            required=False,
            label=Config.lazy_system_value("content_warning_text"),
            widget=forms.TextInput(
                attrs={
                    "placeholder": Config.lazy_system_value("content_warning_text"),
                },
            ),
            help_text="Optional - Playlist will be hidden behind this text until clicked",
        )

        image = forms.ImageField(
            required=False,
            help_text="Optional - For multiple image uploads and cropping, please use an app",
            widget=forms.FileInput(
                attrs={
                    "_": f"""
                        on change
                            if me.files[0].size > {settings.SETUP.MEDIA_MAX_IMAGE_FILESIZE_MB * 1024 ** 2}
                                add [@disabled=] to #upload

                                remove <ul.errorlist/>
                                make <ul.errorlist/> called errorlist
                                make <li/> called error
                                set size_in_mb to (me.files[0].size / 1024 / 1024).toFixed(2)
                                put 'File must be {settings.SETUP.MEDIA_MAX_IMAGE_FILESIZE_MB}MB or less (actual: ' + size_in_mb + 'MB)' into error
                                put error into errorlist
                                put errorlist before me
                            else
                                remove @disabled from #upload
                                remove <ul.errorlist/>
                            end
                        end
                    """
                }
            ),
        )

        image_caption = forms.CharField(
            required=False,
            help_text="Provide an image caption for the visually impaired",
        )

        def __init__(self, identity, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.identity = identity
            self.fields["description"].widget.attrs[
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
                put {Config.system.post_length} - characters into #character-counter

                if characters > {Config.system.post_length} then
                    set #character-counter's style.color to 'var(--color-text-error)'
                    add [@disabled=] to #playlist-button
                else
                    set #character-counter's style.color to ''
                    remove @disabled from #playlist-button
                end
            """

        def clean_text(self):
            text = self.cleaned_data.get("text")
            # Check minimum interval
            last_playlist = self.identity.playlists.order_by("-created").first()
            if (
                last_playlist
                and (timezone.now() - last_playlist.created).total_seconds()
                < Config.system.playlist_minimum_interval
            ):
                raise forms.ValidationError(
                    f"You must wait at least {Config.system.playlist_minimum_interval} seconds between playlists"
                )
            if not text:
                return text
            # Check playlist length
            length = len(text)
            if length > Config.system.playlist_length:
                raise forms.ValidationError(
                    f"Maximum playlist length is {Config.system.playlist_length} characters (you have {length})"
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
        initial["visibility"] = self.identity.config_identity.default_post_visibility
        return initial

    def form_valid(self, form):
        # See if we need to make an image attachment
        attachments = []
        if form.cleaned_data.get("image"):
            main_file = resize_image(
                form.cleaned_data["image"],
                size=(2000, 2000),
                cover=False,
            )
            thumbnail_file = resize_image(
                form.cleaned_data["image"],
                size=(400, 225),
                cover=True,
            )
            attachment = PlaylistAttachment.objects.create(
                blurhash=blurhash_image(thumbnail_file),
                mimetype="image/webp",
                width=main_file.image.width,
                height=main_file.image.height,
                name=form.cleaned_data.get("image_caption"),
                state=PlaylistAttachmentStates.fetched,
                author=self.identity,
            )
            attachment.file.save(
                main_file.name,
                main_file,
            )
            attachment.thumbnail.save(
                thumbnail_file.name,
                thumbnail_file,
            )
            attachment.save()
            attachments.append(attachment)

        # Create the playlist
        playlist = Playlist.create_local(
            author=self.identity,
            name=form.cleaned_data["name"],
            description=form.cleaned_data["description"],
            summary=form.cleaned_data.get("content_warning"),
            visibility=form.cleaned_data["visibility"],
            attachments=attachments,
        )

        # Add their own timeline event for immediate visibility
        TimelineEvent.add_playlist(self.identity, playlist)
        messages.success(self.request, "Your playlist was created.")
        return redirect(".")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["identity"] = self.identity
        context["section"] = "upsert"
        return context
