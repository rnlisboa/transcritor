from django.urls import path

from . import views

app_name = "transcritor"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/videos/upload/", views.upload_video, name="upload"),
    path("api/videos/status/", views.videos_status, name="status"),
    path("api/videos/clear/", views.clear_all_videos, name="clear"),
    path("api/videos/<int:video_id>/transcription/", views.video_transcription, name="transcription"),
    path("api/videos/<int:video_id>/thumbnail/", views.video_thumbnail, name="thumbnail"),
]
