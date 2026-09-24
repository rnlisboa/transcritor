import uuid
from pathlib import Path

from django.db import models


def video_upload_to(instance, filename):
    """Gera um nome aleatório: o nome enviado pelo usuário nunca vai para o disco."""
    extension = Path(filename).suffix.lower()
    return f"videos/{uuid.uuid4().hex}{extension}"


class Video(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Aguardando processamento"
        EXTRACTING_AUDIO = "EXTRACTING_AUDIO", "Extraindo áudio"
        TRANSCRIBING = "TRANSCRIBING", "Transcrevendo"
        COMPLETED = "COMPLETED", "Transcrição concluída"
        ERROR = "ERROR", "Erro no processamento"

    IN_PROGRESS_STATUSES = (Status.EXTRACTING_AUDIO, Status.TRANSCRIBING)
    ACTIVE_STATUSES = (Status.PENDING, *IN_PROGRESS_STATUSES)

    # Identifica a sessão do navegador dona do vídeo (a aplicação não tem login).
    owner_key = models.CharField(max_length=64, db_index=True)
    original_name = models.CharField(max_length=255)
    video_file = models.FileField(upload_to=video_upload_to, max_length=255)
    audio_file = models.FileField(blank=True, max_length=255)
    thumbnail = models.FileField(blank=True, max_length=255)
    transcription = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    # Percentual real (0–100) quando disponível; nulo quando não há métrica confiável.
    progress = models.PositiveSmallIntegerField(null=True, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "vídeo"
        verbose_name_plural = "vídeos"

    def __str__(self):
        return f"{self.original_name} ({self.get_status_display()})"

    def stored_file_names(self):
        """Nomes (relativos ao MEDIA_ROOT) de todos os arquivos ligados ao vídeo."""
        return [f.name for f in (self.video_file, self.audio_file, self.thumbnail) if f and f.name]
