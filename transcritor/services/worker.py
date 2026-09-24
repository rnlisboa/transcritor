"""Worker de processamento baseado no banco de dados.

Executado como processo separado (`python manage.py process_videos`), nunca
dentro de uma requisição HTTP. Processa um vídeo por vez, em ordem de chegada.

- Lock de instância única: impede dois workers ao mesmo tempo na mesma máquina.
- Heartbeat em arquivo: permite à interface avisar quando o worker está parado.
- SIGINT/SIGTERM: encerra de forma limpa; o vídeo em andamento volta para a fila.
"""
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, close_old_connections

from . import ffmpeg, processor
from .errors import ProcessingError
from .transcription import TranscriptionService

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_MAX_AGE_SECONDS = 45


class WorkerAlreadyRunning(Exception):
    pass


def _state_dir():
    path = Path(settings.WORKER_STATE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _heartbeat_path():
    return Path(settings.WORKER_STATE_DIR) / "worker.heartbeat"


def worker_is_online():
    """True se o worker deu sinal de vida recentemente."""
    try:
        last_beat = float(_heartbeat_path().read_text(encoding="ascii"))
    except (OSError, ValueError):
        return False
    return time.time() - last_beat < HEARTBEAT_MAX_AGE_SECONDS


@contextmanager
def single_instance_lock():
    handle = open(_state_dir() / "worker.lock", "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerAlreadyRunning() from exc
        yield
    finally:
        handle.close()  # fechar o arquivo libera o lock


class _Heartbeat(threading.Thread):
    """Atualiza o arquivo de heartbeat mesmo durante tarefas longas (FFmpeg/Whisper)."""

    def __init__(self):
        super().__init__(name="worker-heartbeat", daemon=True)
        self._stopped = threading.Event()

    def run(self):
        path = _heartbeat_path()
        while not self._stopped.is_set():
            try:
                path.write_text(str(time.time()), encoding="ascii")
            except OSError:
                logger.warning("Não foi possível atualizar o heartbeat do worker.", exc_info=True)
            self._stopped.wait(HEARTBEAT_INTERVAL_SECONDS)

    def stop(self):
        self._stopped.set()
        self.join(timeout=5)
        try:
            _heartbeat_path().unlink(missing_ok=True)
        except OSError:
            pass


def _install_signal_handlers(stop_event):
    if threading.current_thread() is not threading.main_thread():
        return

    def handle(signum, _frame):
        if stop_event.is_set():
            raise KeyboardInterrupt  # segundo sinal: encerra imediatamente
        logger.info("Sinal %s recebido: encerrando após a etapa atual...", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)


def _process_next(transcriber, stop_event):
    """Processa o próximo vídeo da fila. Retorna True se havia um vídeo."""
    close_old_connections()
    try:
        video = processor.claim_next_video()
    except DatabaseError:
        logger.exception("Erro ao consultar a fila no banco de dados; nova tentativa em instantes.")
        return False
    if video is None:
        return False
    processor.process_video(video, transcriber, should_stop=stop_event.is_set)
    return True


def run_worker(poll_interval=None, once=False):
    """Loop principal do worker.

    `once=True` processa a fila atual e encerra (útil em tarefas agendadas).
    """
    poll_interval = poll_interval or settings.WORKER_POLL_INTERVAL
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    with single_instance_lock():
        heartbeat = _Heartbeat()
        heartbeat.start()
        try:
            try:
                logger.info("FFmpeg: %s", ffmpeg.check_available())
            except ProcessingError:
                logger.error("FFmpeg não encontrado: os vídeos falharão até que ele seja configurado.")

            processor.requeue_interrupted_videos()

            transcriber = TranscriptionService()
            try:
                transcriber.load_model()  # uma única vez; reutilizado para todos os vídeos
            except ProcessingError:
                logger.error("O modelo será carregado novamente quando houver um vídeo na fila.")

            logger.info(
                "Worker iniciado (modelo=%s, device=%s, compute_type=%s, idioma=%s, intervalo=%ss%s).",
                transcriber.model_name, transcriber.device, transcriber.compute_type,
                transcriber.language or "auto", poll_interval, ", modo --once" if once else "",
            )

            while not stop_event.is_set():
                if _process_next(transcriber, stop_event):
                    continue
                if once:
                    logger.info("Fila vazia.")
                    break
                stop_event.wait(poll_interval)
        finally:
            heartbeat.stop()
            close_old_connections()

    logger.info("Worker encerrado.")
