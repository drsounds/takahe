from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.views.decorators.vary import vary_on_headers
from django.views.generic import TemplateView

from ..models import Playlist, PlaylistStates
from ..services import PlaylistService
from core.decorators import cache_page_by_ap_json
from core.ld import canonicalise
from users.models import Identity
from users.shortcuts import by_handle_or_404


@method_decorator(
    cache_page_by_ap_json("cache_timeout_page_playlist", public_only=True), name="dispatch"
)
@method_decorator(vary_on_headers("Accept"), name="dispatch")
class Individual(TemplateView):
    template_name = "activities/playlist.html"

    identity: Identity
    playlist_obj: Playlist

    def get(self, request, handle, playlist_id):
        self.identity = by_handle_or_404(self.request, handle, local=False)
        if self.identity.blocked:
            raise Http404("Blocked user")
        self.playlist_obj = get_object_or_404(
            PlaylistService.queryset()
            .filter(author=self.identity)
            .unlisted(include_replies=True),
            pk=playlist_id,
        )
        if self.playlist_obj.state in [PlaylistStates.deleted, PlaylistStates.deleted_fanned_out]:
            raise Http404("Deleted playlist")
        # If they're coming in looking for JSON, they want the actor
        if request.ap_json:
            # Return playlist JSON
            return self.serve_object()
        else:
            # Show normal page
            return super().get(request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        ancestors, descendants = PlaylistService(self.playlist_obj).context(
            identity=None, num_ancestors=2
        )

        context.update(
            {
                "identity": self.identity,
                "playlist": self.playlist_obj,
                "link_original": True,
                "ancestors": ancestors,
                "descendants": descendants,
                "public_styling": True,
            }
        )

        return context

    def serve_object(self):
        # If this not a local playlist, redirect to its canonical URI
        if not self.playlist_obj.local:
            return redirect(self.playlist_obj.object_uri)
        return JsonResponse(
            canonicalise(self.playlist_obj.to_ap(), include_security=True),
            content_type="application/activity+json",
        )
