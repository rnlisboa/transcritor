from pathlib import PurePath

from django import forms
from django.conf import settings

# Tipos que navegadores costumam enviar quando não reconhecem o vídeo (ex.: .mkv no Windows).
GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
MP4_BOX_TYPES = {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot"}
MAX_NAME_LENGTH = 200


def sanitize_filename(raw_name):
    """Mantém só o nome-base (sem diretórios) e remove caracteres de controle.

    O resultado serve apenas para exibição: no disco o arquivo recebe um nome aleatório.
    """
    name = (raw_name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if len(name) > MAX_NAME_LENGTH:
        suffix = PurePath(name).suffix[:10]
        name = name[: MAX_NAME_LENGTH - len(suffix)] + suffix
    return name


def detect_video_container(header):
    """Identifica o contêiner pelos primeiros bytes (assinatura), não pela extensão."""
    if len(header) >= 8 and header[4:8] in MP4_BOX_TYPES:
        return "mp4/mov"
    if header[:4] == b"\x1a\x45\xdf\xa3":
        return "webm/mkv"
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "avi"
    return None


class VideoUploadForm(forms.Form):
    file = forms.FileField(
        error_messages={
            "required": "Nenhum arquivo foi enviado.",
            "empty": "O arquivo enviado está vazio.",
            "invalid": "O arquivo enviado é inválido.",
            "missing": "Nenhum arquivo foi enviado.",
        }
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]

        name = sanitize_filename(uploaded.name)
        extension = PurePath(name).suffix.lower()
        if not name or not PurePath(name).stem:
            raise forms.ValidationError("Nome de arquivo inválido.")

        allowed = settings.ALLOWED_VIDEO_EXTENSIONS
        if extension not in allowed:
            formats = ", ".join(ext.lstrip(".").upper() for ext in allowed)
            raise forms.ValidationError(f"Formato não suportado. Envie vídeos nos formatos {formats}.")

        if uploaded.size > settings.MAX_UPLOAD_SIZE_BYTES:
            raise forms.ValidationError(f"O arquivo excede o limite de {settings.MAX_UPLOAD_SIZE_MB} MB.")

        content_type = (uploaded.content_type or "").split(";")[0].strip().lower()
        if content_type not in GENERIC_CONTENT_TYPES and not content_type.startswith("video/"):
            raise forms.ValidationError("O arquivo enviado não é um vídeo.")

        uploaded.seek(0)
        header = uploaded.read(16)
        uploaded.seek(0)
        if detect_video_container(header) is None:
            raise forms.ValidationError("O conteúdo do arquivo não corresponde a um vídeo válido.")

        self.original_name = name
        uploaded.name = name  # o armazenamento usa apenas a extensão já validada
        return uploaded
