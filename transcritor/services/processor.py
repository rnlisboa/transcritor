"""Etapas do processamento de um vídeo, cada uma executada numa requisição curta.

O navegador conduz o fluxo, um vídeo por vez:

    upload (PENDING) → extract_audio (EXTRACTING_AUDIO → TRANSCRIBING)
                     → transcribe_next_chunk, repetido até COMPLETED

Como o estado fica no banco, o processamento pode ser retomado depois se a
página for fechada. As atualizações usam UPDATE condicional (QuerySet.update),
nunca Model.save(): um vídeo removido durante uma etapa não é recriado.
"""
import errno
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.utils import timezone

from ..models import Video
from . import ffmpeg, transcription
from .errors import InvalidStateError, ProcessingError

logger = logging.getLogger(__name__)

GENERIC_ERROR_MESSAGE = "Ocorreu um erro inesperado ao processar o vídeo."
DISK_FULL_MESSAGE = "Não há espaço em disco suficiente no servidor para processar o vídeo."


def delete_stored_files(names):
    """Remove arquivos do MEDIA_ROOT, ignorando os que já não existem."""
    for name in names:
        if not name:
            continue
        try:
            default_storage.delete(name)
        except (OSError, SuspiciousFileOperation):
            logger.warning("Não foi possível remover o arquivo %s.", name, exc_info=True)


def audio_name_for(video):
    return f"audio/{video.pk}.wav"


def download_audio_name_for(video):
    return f"audio/{video.pk}.m4a"


def _friendly_message(exc):
    if isinstance(exc, ProcessingError):
        return exc.user_message
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return DISK_FULL_MESSAGE
    return GENERIC_ERROR_MESSAGE


def _fail(video, exc, keep_download_audio=True):
    """Marca o vídeo como ERROR e apaga vídeo e áudio (não há como reprocessar).

    O áudio para download é mantido quando já existe: a pessoa ainda pode
    transcrevê-lo em outro lugar.
    """
    message = _friendly_message(exc)
    if isinstance(exc, ProcessingError):
        logger.warning("Vídeo #%s: %s", video.pk, message)
    else:
        logger.error("Vídeo #%s: erro inesperado.", video.pk, exc_info=exc)
    fields = dict(
        status=Video.Status.ERROR, error_message=message[:500], progress=None,
        video_file="", audio_file="", updated_at=timezone.now(),
    )
    file_names = [video.video_file.name, video.audio_file.name, audio_name_for(video)]
    if not keep_download_audio:
        fields["download_audio"] = ""
        file_names += [video.download_audio.name, download_audio_name_for(video)]
    Video.objects.filter(pk=video.pk).update(**fields)
    delete_stored_files(file_names)


def extract_audio(video):
    """Extrai o áudio (WAV mono 16 kHz) e apaga o vídeo, que não é mais necessário.

    É rápido mesmo para vídeos longos: o FFmpeg só decodifica a trilha de áudio.
    """
    claimed = Video.objects.filter(
        pk=video.pk, status__in=(Video.Status.PENDING, Video.Status.EXTRACTING_AUDIO)
    ).update(status=Video.Status.EXTRACTING_AUDIO, progress=None, error_message="", updated_at=timezone.now())
    if not claimed:
        raise InvalidStateError()

    audio_name = audio_name_for(video)
    download_name = download_audio_name_for(video)
    download_path = Path(settings.MEDIA_ROOT) / download_name
    try:
        if not video.video_file:
            raise ProcessingError("O arquivo do vídeo não foi encontrado. Envie o vídeo novamente.")
        Video.objects.filter(pk=video.pk).update(audio_file=audio_name)
        ffmpeg.extract_audio(Path(video.video_file.path), Path(settings.MEDIA_ROOT) / audio_name, download_path)
    except Exception as exc:
        _fail(video, exc, keep_download_audio=False)
        return

    has_download = download_path.is_file() and download_path.stat().st_size > 0
    updated = Video.objects.filter(pk=video.pk).update(
        status=Video.Status.TRANSCRIBING, progress=0, audio_position=0, transcription="",
        video_file="", download_audio=download_name if has_download else "", updated_at=timezone.now(),
    )
    if updated:
        delete_stored_files([video.video_file.name] + ([] if has_download else [download_name]))
        logger.info("Vídeo #%s: áudio extraído; vídeo original removido.", video.pk)
    else:  # removido durante a etapa
        delete_stored_files([video.video_file.name, audio_name, download_name])


def transcribe_next_chunk(video):
    """Transcreve o próximo pedaço do áudio e salva o avanço."""
    if video.status != Video.Status.TRANSCRIBING:
        raise InvalidStateError()

    audio_path = Path(video.audio_file.path) if video.audio_file else None
    try:
        if audio_path is None or not audio_path.is_file():
            raise ProcessingError("O áudio extraído não foi encontrado. Envie o vídeo novamente.")
        result = transcription.transcribe_chunk(audio_path, video.audio_position)
    except Exception as exc:
        _fail(video, exc)
        return

    text = " ".join(part for part in (video.transcription, result.text) if part)
    text = text[:1].upper() + text[1:]  # o Vosk devolve tudo em minúsculas
    fields = {
        "transcription": text,
        "audio_position": result.position,
        "progress": min(99, result.position * 100 // max(result.total, 1)),
        "updated_at": timezone.now(),
    }
    if result.finished:
        fields.update(status=Video.Status.COMPLETED, progress=100, audio_file="")

    # Só salva se ninguém avançou este vídeo no meio tempo (ex.: duas abas abertas).
    saved = Video.objects.filter(
        pk=video.pk, status=Video.Status.TRANSCRIBING, audio_position=video.audio_position
    ).update(**fields)

    if saved and result.finished:
        delete_stored_files([video.audio_file.name])
        logger.info("Vídeo #%s: transcrição concluída (%s caracteres).", video.pk, len(text))
    elif not saved and not Video.objects.filter(pk=video.pk).exists():
        delete_stored_files([video.audio_file.name])  # removido durante a etapa


def clear_videos(owner_key):
    """Remove todos os vídeos de um dono: registros e arquivos."""
    videos = list(Video.objects.filter(owner_key=owner_key))
    file_names = []
    for video in videos:
        file_names.extend(video.stored_file_names())
        file_names += [audio_name_for(video), download_audio_name_for(video)]
    Video.objects.filter(pk__in=[v.pk for v in videos]).delete()
    delete_stored_files(file_names)
    logger.info("Limpeza concluída: %s vídeo(s) removido(s).", len(videos))
    return len(videos)
