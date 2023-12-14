from django import forms
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import FormView

from activities.models import Post, PostAttachment, PostAttachmentStates, TimelineEvent
from core.files import blurhash_image, resize_image
from core.models import Config
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
                put {Config.system.post_length} - characters into #character-counter

                if characters > {Config.system.post_length} then
                    set #character-counter's style.color to 'var(--color-text-error)'
                    add [@disabled=] to #post-button
                else
                    set #character-counter's style.color to ''
                    remove @disabled from #post-button
                end
            """

        def clean_text(self):
            text = self.cleaned_data.get("text")
            # Check minimum interval
            last_post = self.identity.posts.order_by("-created").first()
            if (
                last_post
                and (timezone.now() - last_post.created).total_seconds()
                < Config.system.post_minimum_interval
            ):
                raise forms.ValidationError(
                    f"You must wait at least {Config.system.post_minimum_interval} seconds between posts"
                )
            if not text:
                return text
            # Check post length
            length = len(text)
            if length > Config.system.post_length:
                raise forms.ValidationError(
                    f"Maximum post length is {Config.system.post_length} characters (you have {length})"
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
            attachment = PostAttachment.objects.create(
                blurhash=blurhash_image(thumbnail_file),
                mimetype="image/webp",
                width=main_file.image.width,
                height=main_file.image.height,
                name=form.cleaned_data.get("image_caption"),
                state=PostAttachmentStates.fetched,
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
        # Create the post
        post = Post.create_local(
            author=self.identity,
            content=form.cleaned_data["text"],
            summary=form.cleaned_data.get("content_warning"),
            visibility=form.cleaned_data["visibility"],
            attachments=attachments,
        )
        # Add their own timeline event for immediate visibility
        TimelineEvent.add_post(self.identity, post)
        messages.success(self.request, "Your post was created.")
        return redirect(".")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["identity"] = self.identity
        context["section"] = "upsert_playlist_item"
        return context

