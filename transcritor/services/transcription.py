"""Transcrição de áudio com Vosk, em pedaços curtos.

Cada chamada de `transcribe_chunk` processa o áudio a partir de uma posição
até passar ~TRANSCRIBE_CHUNK_SECONDS de processamento e parar na próxima pausa
da fala. Assim cada requisição HTTP é curta e a transcrição pode ser retomada
de onde parou. O modelo é carregado uma vez por processo e reaproveitado.
"""
import json
import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .errors import ProcessingError

logger = logging.getLogger(__name__)

BLOCK_SECONDS = 0.25
# Se a fala não fizer pausa, corta mesmo assim após o dobro do tempo previsto.
HARD_LIMIT_FACTOR = 2

_model = None
_model_lock = threading.Lock()


class ModelUnavailableError(ProcessingError):
    default_message = "O modelo de transcrição não está disponível no servidor. Avise o administrador."


class TranscriptionError(ProcessingError):
    default_message = "Não foi possível transcrever o áudio do vídeo."


@dataclass
class ChunkResult:
    text: str
    position: int
    total: int

    @property
    def finished(self):
        return self.position >= self.total


def get_model():
    """Carrega o modelo Vosk uma única vez por processo."""
    global _model
    with _model_lock:
        if _model is not None:
            return _model

        model_path = Path(settings.VOSK_MODEL_PATH)
        if not model_path.is_dir():
            logger.error(
                "Modelo Vosk não encontrado em %s. Rode 'python manage.py download_vosk_model' "
                "ou ajuste VOSK_MODEL_PATH.", model_path,
            )
            raise ModelUnavailableError()
        try:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)  # silencia o log interno (Kaldi)
            started = time.monotonic()
            _model = Model(str(model_path))
        except Exception as exc:
            logger.exception("Falha ao carregar o modelo Vosk de %s.", model_path)
            raise ModelUnavailableError() from exc
        logger.info("Modelo Vosk carregado de %s em %.1fs.", model_path, time.monotonic() - started)
        return _model


def _text_of(result_json):
    return json.loads(result_json).get("text", "").strip()


def transcribe_chunk(audio_path, start_position, chunk_seconds=None):
    """Transcreve a partir de `start_position` (em amostras) e devolve até onde chegou."""
    model = get_model()
    from vosk import KaldiRecognizer  # disponível: get_model() já importou o vosk

    chunk_seconds = chunk_seconds or settings.TRANSCRIBE_CHUNK_SECONDS
    try:
        with wave.open(str(audio_path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise TranscriptionError("O áudio extraído está em um formato inesperado.")
            rate = wav.getframerate()
            total = wav.getnframes()
            position = min(start_position, total)
            wav.setpos(position)

            recognizer = KaldiRecognizer(model, rate)
            block_frames = int(rate * BLOCK_SECONDS)
            texts = []
            started = time.monotonic()

            while position < total:
                data = wav.readframes(block_frames)
                if not data:
                    break
                position += len(data) // 2
                elapsed = time.monotonic() - started
                if recognizer.AcceptWaveform(data):
                    texts.append(_text_of(recognizer.Result()))
                    if elapsed >= chunk_seconds:
                        break  # parou numa pausa natural da fala
                elif elapsed >= chunk_seconds * HARD_LIMIT_FACTOR:
                    break

            texts.append(_text_of(recognizer.FinalResult()))
    except ProcessingError:
        raise
    except (wave.Error, EOFError) as exc:
        logger.exception("Áudio inválido em %s.", audio_path)
        raise TranscriptionError("O áudio extraído está corrompido.") from exc
    except MemoryError as exc:
        logger.exception("Memória insuficiente ao transcrever %s.", audio_path)
        raise TranscriptionError("Memória insuficiente para transcrever este vídeo.") from exc
    except OSError:
        raise
    except Exception as exc:
        logger.exception("Falha do Vosk ao transcrever %s.", audio_path)
        raise TranscriptionError() from exc

    return ChunkResult(text=" ".join(t for t in texts if t), position=position, total=total)
