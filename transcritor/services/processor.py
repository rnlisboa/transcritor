"""Processamento de um vídeo: extração de áudio -> transcrição -> conclusão.

Todas as alterações de status usam QuerySet.update() (nunca Model.save()):
assim, se o vídeo for removido durante o processamento, a atualização afeta
0 linhas e o processamento é abortado, em vez de recriar o registro apagado.
"""
import errno
import logging
import time
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.utils import timezone

from ..models import Video
from . import ffmpeg
from .errors import ProcessingError, VideoDeletedError, WorkerStopRequested

logger = logging.getLogger(__name__)

GENERIC_ERROR_MESSAGE = "Ocorreu um erro inesperado ao processar o vídeo."
DISK_FULL_MESSAGE = "Não há espaço em disco suficiente no servidor para processar o vídeo."
PROGRESS_SAVE_INTERVAL_SECONDS = 3


def _update(video_id, **fields):
    """Atualiza o vídeo; levanta VideoDeletedError se ele não existir mais."""
    fields["updated_at"] = timezone.now()
    if not Video.objects.filter(pk=video_id).update(**fields):
        raise VideoDeletedError()


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
    return f"audio/{Path(video.video_file.name).stem}.wav"


def claim_next_video():
    """Reserva o vídeo pendente mais antigo de forma atômica.

    A troca PENDING -> EXTRACTING_AUDIO é um UPDATE condicional
    (WHERE status = 'PENDING'): se dois workers tentarem pegar o mesmo vídeo,
    apenas um deles consegue atualizar a linha.
    """
    while True:
        candidate_id = (
            Video.objects.filter(status=Video.Status.PENDING)
            .order_by("created_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
        claimed = Video.objects.filter(pk=candidate_id, status=Video.Status.PENDING).update(
            status=Video.Status.EXTRACTING_AUDIO,
            progress=None,
            error_message="",
            updated_at=timezone.now(),
        )
        if claimed:
            try:
                return Video.objects.get(pk=candidate_id)
            except Video.DoesNotExist:
                continue  # removido logo após a reserva


def requeue_interrupted_videos():
    """Devolve para a fila vídeos que ficaram "em andamento" após uma queda do worker.

    Só deve ser chamada por um worker que detém o lock de instância única.
    """
    interrupted = list(Video.objects.filter(status__in=Video.IN_PROGRESS_STATUSES))
    for video in interrupted:
        delete_stored_files([video.audio_file.name])
    count = Video.objects.filter(pk__in=[v.pk for v in interrupted]).update(
        status=Video.Status.PENDING, progress=None, audio_file="", updated_at=timezone.now()
    )
    if count:
        logger.warning("%s vídeo(s) interrompido(s) voltaram para a fila.", count)
    return count


def clear_videos(owner_key):
    """Remove todos os vídeos de um dono: registros primeiro, depois os arquivos.

    Se o worker estiver processando um desses vídeos, ele percebe a remoção na
    próxima atualização de status, interrompe o trabalho e apaga o que sobrou.
    """
    videos = list(Video.objects.filter(owner_key=owner_key))
    file_names = []
    for video in videos:
        file_names.extend(video.stored_file_names())
        file_names.append(audio_name_for(video))
    Video.objects.filter(pk__in=[v.pk for v in videos]).delete()
    delete_stored_files(file_names)
    logger.info("Limpeza concluída: %s vídeo(s) removido(s).", len(videos))
    return len(videos)


class _ProgressReporter:
    """Salva o percentual da transcrição no banco sem escrever a cada segmento."""

    def __init__(self, video_id, should_stop):
        self.video_id = video_id
        self.should_stop = should_stop
        self.last_saved_at = 0.0

    def __call__(self, percent):
        if self.should_stop():
            raise WorkerStopRequested()
        now = time.monotonic()
        if now - self.last_saved_at >= PROGRESS_SAVE_INTERVAL_SECONDS:
            _update(self.video_id, progress=percent)
            self.last_saved_at = now


def process_video(video, transcriber, should_stop=lambda: False):
    """Processa um vídeo já reservado (status EXTRACTING_AUDIO).

    Nunca levanta exceção: o resultado fica registrado no banco.
    """
    audio_name = audio_name_for(video)
    audio_path = Path(settings.MEDIA_ROOT) / audio_name
    started = time.monotonic()
    logger.info("Vídeo #%s (%s): iniciando processamento.", video.pk, video.original_name)

    try:
        _update(video.pk, audio_file=audio_name)
        logger.info("Vídeo #%s: extraindo áudio.", video.pk)
        ffmpeg.extract_audio(Path(video.video_file.path), audio_path)
        if should_stop():
            raise WorkerStopRequested()

        _update(video.pk, status=Video.Status.TRANSCRIBING, progress=0)
        logger.info("Vídeo #%s: transcrevendo.", video.pk)
        text = transcriber.transcribe(audio_path, on_progress=_ProgressReporter(video.pk, should_stop))

        _update(
            video.pk,
            status=Video.Status.COMPLETED,
            transcription=text,
            progress=100,
            audio_file="",
            error_message="",
        )
        logger.info("Vídeo #%s: concluído em %.1fs (%s caracteres).", video.pk, time.monotonic() - started, len(text))
    except VideoDeletedError:
        logger.info("Vídeo #%s foi removido durante o processamento; limpando arquivos.", video.pk)
        delete_stored_files(video.stored_file_names())
    except WorkerStopRequested:
        _requeue(video)
    except ProcessingError as exc:
        if should_stop():
            _requeue(video)  # ex.: FFmpeg interrompido pelo mesmo Ctrl+C
        else:
            logger.warning("Vídeo #%s: erro no processamento: %s", video.pk, exc.user_message)
            _mark_error(video, exc.user_message)
    except Exception as exc:
        if should_stop():
            _requeue(video)
        else:
            logger.exception("Vídeo #%s: erro inesperado no processamento.", video.pk)
            is_disk_full = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
            _mark_error(video, DISK_FULL_MESSAGE if is_disk_full else GENERIC_ERROR_MESSAGE)
    finally:
        # O áudio é temporário: nunca permanece após o processamento.
        try:
            audio_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Não foi possível remover o áudio temporário %s.", audio_path, exc_info=True)


def _mark_error(video, message):
    try:
        _update(video.pk, status=Video.Status.ERROR, error_message=message[:500], progress=None, audio_file="")
    except VideoDeletedError:
        delete_stored_files(video.stored_file_names())
    except Exception:
        logger.exception("Vídeo #%s: não foi possível registrar o erro no banco.", video.pk)


def _requeue(video):
    logger.warning("Vídeo #%s: processamento interrompido; o vídeo voltará para a fila.", video.pk)
    try:
        _update(video.pk, status=Video.Status.PENDING, progress=None, audio_file="")
    except VideoDeletedError:
        delete_stored_files(video.stored_file_names())
    except Exception:
        logger.exception("Vídeo #%s: não foi possível devolver o vídeo para a fila.", video.pk)
