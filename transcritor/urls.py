from django.urls import path

from . import views

app_name = "transcritor"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/videos/upload/", views.upload_video, name="upload"),
    path("api/videos/clear/", views.clear_all_videos, name="clear"),
    path("api/videos/<int:video_id>/extract/", views.extract_audio, name="extract"),
    path("api/videos/<int:video_id>/transcribe/", views.transcribe_chunk, name="transcribe"),
    path("api/videos/<int:video_id>/thumbnail/", views.video_thumbnail, name="thumbnail"),
    path("api/videos/<int:video_id>/audio/", views.video_audio, name="audio"),
]
