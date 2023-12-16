import uuid

from django.db import models

from django.contrib.auth.models import User


class Node(models.Model):
    id = models.UUIDField(default=uuid.uuid4, primary_key=True, edit=False)

    class Meta:
        abstract = True


class Entity(Node):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    slug = models.SlugField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name or 'Unnamed'

    class Meta:
        abstract = True


class Creator(Entity):
    def to_json(self):
        return {
            "type": "Creator",
            "name": self.name
        }


class Release(Entity):
    creators = models.ManyToManyField(Creator, blank=True, null=True, related_name='+')
    upc = models.CharField(max_length=255, null=True, blank=True)

    def to_json(self):
        return {
            "type": "Release",
            "name": self.name,
            "version": self.version,
            "upc": self.upc,
            "creators": [ 
                creator.to_json()
                for
                creator
                in
                self.creators.all()
            ]
        }


class Recording(Entity):
    creators = models.ManyToManyField(Creator, blank=True, null=True, related_name='+')
    isrc = models.CharField(max_length=255, null=True, blank=True)
    version = models.CharField(max_length=255, null=True, blank=True)

    def to_json(self):
        return {
            "type": "Recording",
            "name": self.name,
            "version": self.version,
            "isrc": self.isrc,
            "creators": [ 
                creator.to_json()
                for
                creator
                in
                self.creators.all()
            ]
        }


class Track(Entity):
    recording = models.ForeignKey(Recording, on_delete=models.CASCADE, related_name='+')
    release = models.ForeignKey(Release, on_delete=models.CASCADE, reated_name='+')
    number = models.IntegerField(default=0)

    def to_json(self):
        return {
            "type": "Track",
            "type": "track",
            "number": self.number,
            "release": self.release.to_dict(),
            "recording": self.recording.to_dict(),
        }
