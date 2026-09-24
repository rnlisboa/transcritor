"""Transcrição de áudio com Faster-Whisper.

O modelo é carregado uma única vez por instância de TranscriptionService e
reutilizado para todos os vídeos processados pelo worker.
"""
import logging
import time

from django.conf import settings

from .errors import ProcessingAborted, ProcessingError

logger = logging.getLogger(__name__)


class ModelUnavailableError(ProcessingError):
    default_message = (
        "Não foi possível carregar o modelo de transcrição. "
        "Verifique a configuração WHISPER_MODEL e o acesso ao download do modelo."
    )


class TranscriptionError(ProcessingError):
    default_message = "Não foi possível transcrever o áudio do vídeo."


class TranscriptionService:
    def __init__(self, model_name=None, device=None, compute_type=None, language=None):
        self.model_name = model_name or settings.WHISPER_MODEL
        self.device = device or settings.WHISPER_DEVICE
        self.compute_type = compute_type or settings.WHISPER_COMPUTE_TYPE
        language = language if language is not None else settings.WHISPER_LANGUAGE
        # "auto" (ou vazio) deixa o Whisper detectar o idioma.
        self.language = None if language.lower() in {"", "auto"} else language
        self._model = None

    def load_model(self):
        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            logger.error("Pacote faster-whisper não instalado: %s", exc)
            raise ModelUnavailableError() from exc

        logger.info(
            "Carregando modelo Whisper '%s' (device=%s, compute_type=%s). "
            "Na primeira execução o modelo pode ser baixado; isso pode demorar.",
            self.model_name, self.device, self.compute_type,
        )
        started = time.monotonic()
        try:
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        except Exception as exc:
            logger.exception("Falha ao carregar o modelo Whisper '%s'.", self.model_name)
            raise ModelUnavailableError() from exc
        logger.info("Modelo carregado em %.1fs.", time.monotonic() - started)
        return self._model

    def transcribe(self, audio_path, on_progress=None):
        """Transcreve o áudio e devolve o texto com os segmentos em ordem.

        `on_progress(percent)` é chamado a cada segmento com a posição real já
        transcrita do áudio (0–99). Pode levantar ProcessingAborted para interromper.
        """
        model = self.load_model()
        try:
            segments, info = model.transcribe(str(audio_path), language=self.language)
            duration = info.duration or 0
            texts = []
            for segment in segments:
                text = segment.text.strip()
                if text:
                    texts.append(text)
                if on_progress and duration > 0:
                    on_progress(min(99, int(segment.end / duration * 100)))
        except (ProcessingAborted, ProcessingError):
            raise
        except MemoryError as exc:
            logger.exception("Memória insuficiente ao transcrever %s.", audio_path)
            raise TranscriptionError("Memória insuficiente para transcrever este vídeo.") from exc
        except Exception as exc:
            logger.exception("Falha do Faster-Whisper ao transcrever %s.", audio_path)
            raise TranscriptionError() from exc
        return " ".join(texts)
