"""Views da página única e da API JSON consumida pelo frontend.

Não há worker nem fila em segundo plano: o navegador envia um vídeo e depois
pede cada etapa (extrair áudio, transcrever o próximo pedaço) em requisições
curtas, um vídeo de cada vez. Todo o estado fica no banco.
"""
import errno
import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.http import FileResponse, Http404, JsonResponse, UnreadablePostError
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .forms import VideoUploadForm
from .models import Video
from .services import ffmpeg, processor
from .services.errors import InvalidStateError

logger = logging.getLogger(__name__)

OWNER_SESSION_KEY = "owner_key"


def _owner_key(request, create=False):
    """Chave da sessão que identifica os vídeos deste navegador."""
    key = request.session.get(OWNER_SESSION_KEY)
    if not key and create:
        key = uuid.uuid4().hex
        request.session[OWNER_SESSION_KEY] = key
    return key


def _own_video(request, video_id):
    return get_object_or_404(Video, pk=video_id, owner_key=_owner_key(request) or "")


def _json_error(message, status, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def _serialize(video):
    return {
        "id": video.pk,
        "name": video.original_name,
        "status": video.status,
        "progress": video.progress,
        "error": video.error_message or None,
        "transcription": video.transcription,
        "thumbnail_url": reverse("transcritor:thumbnail", args=[video.pk]) if video.thumbnail else None,
        "audio_url": reverse("transcritor:audio", args=[video.pk]) if video.download_audio else None,
    }


@require_GET
@ensure_csrf_cookie
def index(request):
    owner_key = _owner_key(request, create=True)
    urls = {
        name: reverse(f"transcritor:{name}", args=[0])
        for name in ("extract", "transcribe")
    }
    urls.update(upload=reverse("transcritor:upload"), clear=reverse("transcritor:clear"))
    context = {
        "app_config": {
            "maxUploadSizeBytes": settings.MAX_UPLOAD_SIZE_BYTES,
            "maxUploadSizeMb": settings.MAX_UPLOAD_SIZE_MB,
            "allowedExtensions": settings.ALLOWED_VIDEO_EXTENSIONS,
            "urls": urls,
            "videos": [_serialize(v) for v in Video.objects.filter(owner_key=owner_key)],
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
    return JsonResponse({"video": _serialize(video)}, status=201)


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


def _run_step(request, video_id, step):
    """Executa uma etapa do processamento e devolve o estado atualizado do vídeo.

    Erros de processamento não viram erro HTTP: ficam registrados no vídeo
    (status ERROR + mensagem amigável) e voltam no JSON.
    """
    video = _own_video(request, video_id)
    try:
        step(video)
    except InvalidStateError:
        video.refresh_from_db()
        return _json_error("Esta etapa não se aplica ao estado atual do vídeo.", 409, video=_serialize(video))
    except DatabaseError:
        logger.exception("Erro de banco ao processar o vídeo #%s.", video_id)
        return _json_error("O servidor está ocupado. Tentando novamente...", 503)
    try:
        video.refresh_from_db()
    except Video.DoesNotExist:
        raise Http404
    return JsonResponse({"video": _serialize(video)})


@require_POST
def extract_audio(request, video_id):
    return _run_step(request, video_id, processor.extract_audio)


@require_POST
def transcribe_chunk(request, video_id):
    return _run_step(request, video_id, processor.transcribe_next_chunk)


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
def video_thumbnail(request, video_id):
    video = _own_video(request, video_id)
    if not video.thumbnail:
        raise Http404
    try:
        response = FileResponse(video.thumbnail.open("rb"), content_type="image/jpeg")
    except (OSError, ValueError):
        raise Http404
    response["Cache-Control"] = "private, max-age=86400"
    return response


@require_GET
def video_audio(request, video_id):
    """Baixa o áudio do vídeo (M4A), com o nome do vídeo original."""
    video = _own_video(request, video_id)
    if not video.download_audio:
        raise Http404
    filename = f"{Path(video.original_name).stem or 'audio'}.m4a"
    try:
        return FileResponse(
            video.download_audio.open("rb"), as_attachment=True, filename=filename, content_type="audio/mp4"
        )
    except (OSError, ValueError):
        raise Http404
