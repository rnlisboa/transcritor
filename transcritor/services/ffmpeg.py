"""Chamadas ao FFmpeg via subprocess (sempre com lista de argumentos, nunca shell)."""
import logging
import subprocess
from pathlib import Path

from django.conf import settings

from .errors import ProcessingError

logger = logging.getLogger(__name__)

THUMBNAIL_TIMEOUT_SECONDS = 30
# Roda dentro de uma requisição: precisa terminar antes do limite do servidor
# (o PythonAnywhere encerra requisições com mais de 5 minutos).
EXTRACTION_TIMEOUT_SECONDS = 240
WAV_HEADER_SIZE = 44


class FFmpegNotFoundError(ProcessingError):
    default_message = (
        "O FFmpeg não foi encontrado no servidor. "
        "Verifique a instalação ou a configuração FFMPEG_BINARY."
    )


class FFmpegError(ProcessingError):
    default_message = "Não foi possível processar o vídeo com o FFmpeg."


# Trechos do stderr do FFmpeg -> mensagem amigável.
_KNOWN_ERRORS = (
    ("no space left on device", "Não há espaço em disco suficiente no servidor para processar o vídeo."),
    ("matches no streams", "O vídeo não possui trilha de áudio para transcrever."),
    ("does not contain any stream", "O vídeo não possui trilha de áudio para transcrever."),
    ("no such file or directory", "O arquivo do vídeo não foi encontrado."),
    ("moov atom not found", "O arquivo de vídeo está incompleto ou corrompido."),
    ("invalid data found", "O arquivo está corrompido ou não é um vídeo válido."),
)


def _friendly_message(stderr, fallback):
    lowered = stderr.lower()
    for fragment, message in _KNOWN_ERRORS:
        if fragment in lowered:
            return message
    return fallback


def _run(args, timeout, failure_message):
    command = [settings.FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        logger.error("FFmpeg indisponível (FFMPEG_BINARY=%r): %s", settings.FFMPEG_BINARY, exc)
        raise FFmpegNotFoundError() from exc
    except subprocess.TimeoutExpired as exc:
        logger.error("FFmpeg excedeu o tempo limite de %ss: %s", timeout, command)
        raise FFmpegError("O processamento do vídeo demorou mais que o permitido.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        logger.error("FFmpeg terminou com código %s. Comando: %s. Saída: %s", exc.returncode, command, stderr[-2000:])
        raise FFmpegError(_friendly_message(stderr, failure_message)) from exc


def extract_audio(video_path, audio_path, download_path=None):
    """Extrai a primeira trilha de áudio como WAV mono 16 kHz, o formato que o Vosk espera.

    Mono/16 kHz ocupa ~1,9 MB por minuto, sem perda relevante para transcrição.
    Com download_path, gera na mesma chamada (o vídeo é lido uma vez só) uma cópia
    M4A/AAC mono a 64 kbps (~0,5 MB por minuto) para a pessoa baixar.
    """
    video_path, audio_path = Path(video_path), Path(audio_path)
    if not video_path.is_file():
        raise ProcessingError("O arquivo do vídeo não foi encontrado.")
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    download_args = []
    if download_path is not None:
        download_path = Path(download_path)
        download_path.parent.mkdir(parents=True, exist_ok=True)
        download_args = [
            "-map", "0:a:0",
            "-vn", "-sn", "-dn",
            "-ac", "1",
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",
            str(download_path),
        ]

    _run(
        [
            "-i", str(video_path),
            "-map", "0:a:0",
            "-vn", "-sn", "-dn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(audio_path),
            *download_args,
        ],
        timeout=EXTRACTION_TIMEOUT_SECONDS,
        failure_message="Não foi possível extrair o áudio do vídeo.",
    )

    if not audio_path.is_file() or audio_path.stat().st_size <= WAV_HEADER_SIZE:
        raise FFmpegError("O vídeo não possui áudio que possa ser transcrito.")
    return audio_path


def generate_thumbnail(video_path, thumbnail_path):
    """Gera uma imagem JPEG de um frame do vídeo. Nunca levanta exceção.

    Tenta o frame em 00:00:01; se o vídeo for curto demais, usa o primeiro frame.
    Retorna True se a thumbnail foi criada.
    """
    thumbnail_path = Path(thumbnail_path)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)

    for seek in ("00:00:01", "00:00:00"):
        try:
            _run(
                [
                    "-ss", seek,
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", "scale=640:-2",
                    "-q:v", "4",
                    str(thumbnail_path),
                ],
                timeout=THUMBNAIL_TIMEOUT_SECONDS,
                failure_message="Não foi possível gerar a thumbnail.",
            )
        except FFmpegNotFoundError:
            return False
        except ProcessingError:
            continue
        if thumbnail_path.is_file() and thumbnail_path.stat().st_size > 0:
            return True

    logger.warning("Não foi possível gerar a thumbnail de %s; será usado o placeholder.", video_path)
    thumbnail_path.unlink(missing_ok=True)
    return False
