from django.urls import path
from .views.playlists import create
from .views import playlists
from .views.playlists import items

app_name = 'music'

urlpatterns = [
    path("@<handle>/playlists/create", create.Create.as_view(), name="create"),
    path("@<handle>/playlists/<int:post_id>/", playlists.Individual.as_view()),
    path("@<handle>/playlists/<int:post_id>/upsert", items.upsert.Upsert.as_view(), name="upsert"),
]
