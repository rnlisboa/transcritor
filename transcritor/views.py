"""Views da página única e da API JSON consumida pelo frontend.

O upload apenas salva o arquivo, gera a thumbnail (um único frame, rápido) e
deixa o vídeo como PENDING. A transcrição acontece no worker
(`python manage.py process_videos`), nunca dentro de uma requisição.
"""
import errno
import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.db.models.functions import Length
from django.http import FileResponse, Http404, JsonResponse, UnreadablePostError
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .forms import VideoUploadForm
from .models import Video
from .services import ffmpeg, processor
from .services.worker import worker_is_online

logger = logging.getLogger(__name__)

OWNER_SESSION_KEY = "owner_key"


def _owner_key(request, create=False):
    """Chave da sessão que identifica os vídeos deste navegador."""
    key = request.session.get(OWNER_SESSION_KEY)
    if not key and create:
        key = uuid.uuid4().hex
        request.session[OWNER_SESSION_KEY] = key
    return key


def _json_error(message, status):
    return JsonResponse({"error": message}, status=status)


def _serialize(video, queue_position=None):
    return {
        "id": video.pk,
        "name": video.original_name,
        "status": video.status,
        "status_label": video.get_status_display(),
        "progress": video.progress,
        "error": video.error_message or None,
        "is_completed": video.status == Video.Status.COMPLETED,
        "has_transcription": bool(getattr(video, "transcription_length", len(video.transcription))),
        "thumbnail_url": reverse("transcritor:thumbnail", args=[video.pk]) if video.thumbnail else None,
        "queue_position": queue_position,
    }


def _queue_positions():
    """Quantos vídeos estão à frente de cada vídeo ativo na fila global do worker."""
    active = list(
        Video.objects.filter(status__in=Video.ACTIVE_STATUSES)
        .order_by("created_at", "id")
        .values_list("id", "status")
    )
    ordered = [vid for vid, status in active if status != Video.Status.PENDING]
    ordered += [vid for vid, status in active if status == Video.Status.PENDING]
    return {vid: index for index, vid in enumerate(ordered)}


def _status_payload(owner_key):
    videos = []
    if owner_key:
        videos = list(
            Video.objects.filter(owner_key=owner_key)
            .defer("transcription")
            .annotate(transcription_length=Length("transcription"))
        )
    positions = _queue_positions() if any(v.status in Video.ACTIVE_STATUSES for v in videos) else {}
    return {
        "videos": [_serialize(v, positions.get(v.pk)) for v in videos],
        "worker_online": worker_is_online(),
    }


@require_GET
@ensure_csrf_cookie
def index(request):
    owner_key = _owner_key(request, create=True)
    context = {
        "app_config": {
            "maxUploadSizeBytes": settings.MAX_UPLOAD_SIZE_BYTES,
            "maxUploadSizeMb": settings.MAX_UPLOAD_SIZE_MB,
            "allowedExtensions": settings.ALLOWED_VIDEO_EXTENSIONS,
            "urls": {
                "upload": reverse("transcritor:upload"),
                "status": reverse("transcritor:status"),
                "clear": reverse("transcritor:clear"),
                "transcription": reverse("transcritor:transcription", args=[0]),
            },
            "initialStatus": _status_payload(owner_key),
        },
        "accept": ",".join(settings.ALLOWED_VIDEO_EXTENSIONS),
        "formats_label": ", ".join(ext.lstrip(".").upper() for ext in settings.ALLOWED_VIDEO_EXTENSIONS),
        "max_upload_size_mb": settings.MAX_UPLOAD_SIZE_MB,
    }
    return render(request, "transcritor/index.html", context)


@require_POST
def upload_video(request):
    owner_key = _owner_key(request, create=True)

    try:
        form = VideoUploadForm(request.POST, request.FILES)
        is_valid = form.is_valid()
    except UnreadablePostError:
        logger.warning("Upload interrompido pelo cliente.")
        return _json_error("O envio foi interrompido. Tente novamente.", 400)
    except OSError as exc:
        return _storage_error_response(exc)

    if not is_valid:
        message = next(iter(form.errors.get("file") or []), "Arquivo inválido.")
        logger.info("Upload recusado (%s): %s", request.FILES.get("file"), message)
        return _json_error(message, 400)

    try:
        video = Video(owner_key=owner_key, original_name=form.original_name)
        video.video_file = form.cleaned_data["file"]
        video.save()
    except OSError as exc:
        return _storage_error_response(exc)
    except DatabaseError:
        logger.exception("Erro de banco ao registrar o upload.")
        return _json_error("Não foi possível registrar o vídeo agora. Tente novamente em instantes.", 503)

    _attach_thumbnail(video)
    logger.info("Vídeo #%s recebido: %s (%s bytes).", video.pk, video.original_name, video.video_file.size)
    return JsonResponse({"video": _serialize(video, queue_position=_queue_positions().get(video.pk))}, status=201)


def _attach_thumbnail(video):
    """Gera a thumbnail; em caso de falha o frontend usa um placeholder."""
    thumbnail_name = f"thumbnails/{Path(video.video_file.name).stem}.jpg"
    if ffmpeg.generate_thumbnail(video.video_file.path, Path(settings.MEDIA_ROOT) / thumbnail_name):
        Video.objects.filter(pk=video.pk).update(thumbnail=thumbnail_name)
        video.thumbnail.name = thumbnail_name


def _storage_error_response(exc):
    if exc.errno == errno.ENOSPC:
        logger.error("Sem espaço em disco para salvar o upload.")
        return _json_error("Não há espaço em disco suficiente no servidor para receber este vídeo.", 507)
    logger.exception("Erro de disco ao salvar o upload.")
    return _json_error("Não foi possível salvar o vídeo no servidor. Tente novamente.", 500)


@require_GET
def videos_status(request):
    try:
        payload = _status_payload(_owner_key(request))
    except DatabaseError:
        logger.exception("Erro de banco ao consultar o status.")
        return _json_error("Não foi possível consultar o status agora.", 503)
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def clear_all_videos(request):
    owner_key = _owner_key(request)
    if not owner_key:
        return JsonResponse({"removed": 0})
    try:
        removed = processor.clear_videos(owner_key)
    except DatabaseError:
        logger.exception("Erro de banco ao limpar os vídeos.")
        return _json_error("Não foi possível limpar os vídeos agora. Tente novamente.", 503)
    return JsonResponse({"removed": removed})


@require_GET
def video_transcription(request, video_id):
    video = get_object_or_404(Video, pk=video_id, owner_key=_owner_key(request) or "")
    if video.status != Video.Status.COMPLETED:
        return _json_error("A transcrição ainda não está pronta.", 409)
    return JsonResponse({"id": video.pk, "transcription": video.transcription})


@require_GET
def video_thumbnail(request, video_id):
    video = get_object_or_404(Video, pk=video_id, owner_key=_owner_key(request) or "")
    if not video.thumbnail:
        raise Http404
    try:
        response = FileResponse(video.thumbnail.open("rb"), content_type="image/jpeg")
    except (OSError, ValueError):
        raise Http404
    response["Cache-Control"] = "private, max-age=86400"
    return response
