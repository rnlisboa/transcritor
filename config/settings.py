"""Configurações do projeto Transcritor da Gabi.

Todos os valores sensíveis ou dependentes do ambiente vêm de variáveis de
ambiente (ou do arquivo .env na raiz do projeto). Os padrões permitem rodar
localmente sem nenhuma configuração.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Carrega um arquivo .env simples (CHAVE=valor) sem dependências extras.

    Variáveis já definidas no ambiente têm prioridade.
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def env_str(name, default=""):
    return os.environ.get(name, default).strip()


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"A variável {name} deve ser um número inteiro.") from exc


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# --- Núcleo -----------------------------------------------------------------

DEBUG = env_bool("DEBUG", False)

SECRET_KEY = env_str("SECRET_KEY")
if not SECRET_KEY or SECRET_KEY == "change-me":
    if not DEBUG:
        raise ImproperlyConfigured("Defina SECRET_KEY no ambiente ou no arquivo .env quando DEBUG=False.")
    SECRET_KEY = "django-insecure-somente-para-desenvolvimento-local"

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "transcritor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Antes do CSRF: recusa uploads grandes demais sem ler o corpo da requisição.
    "transcritor.middleware.UploadSizeLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Banco de dados ------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 30},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Internacionalização ---------------------------------------------------------

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

# --- Arquivos estáticos e mídia ----------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Os arquivos de mídia NÃO são servidos publicamente: as thumbnails passam por
# uma view que confere o dono do vídeo. Não é necessário mapear /media/.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env_str("MEDIA_ROOT") or BASE_DIR / "media")

# --- Sessão e segurança -------------------------------------------------------------
# A sessão identifica "de quem" são os vídeos (não há login).

SESSION_COOKIE_AGE = 60 * 60 * 24 * 365
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", not DEBUG)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# --- Upload -------------------------------------------------------------------------

MAX_UPLOAD_SIZE_MB = env_int("MAX_UPLOAD_SIZE_MB", 200)
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
ALLOWED_VIDEO_EXTENSIONS = [".mp4", ".webm", ".mov", ".mkv", ".avi"]
# Arquivos acima de 5 MB vão para um arquivo temporário em disco, não para a memória.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- FFmpeg -------------------------------------------------------------------------

FFMPEG_BINARY = env_str("FFMPEG_BINARY") or "ffmpeg"

# --- Vosk (transcrição) ---------------------------------------------------------------
# Pasta do modelo descompactado. Baixe com: python manage.py download_vosk_model

VOSK_MODEL_PATH = Path(env_str("VOSK_MODEL_PATH") or BASE_DIR / "models" / "vosk-model-small-pt-0.3")
# Cada requisição de transcrição processa áudio por ~N segundos e para na próxima
# pausa da fala. Mantém as requisições curtas (o PythonAnywhere corta em 5 minutos).
TRANSCRIBE_CHUNK_SECONDS = max(2, env_int("TRANSCRIBE_CHUNK_SECONDS", 10))

# --- Logs -----------------------------------------------------------------------------
# Tudo vai para o console (stdout/stderr). No PythonAnywhere isso aparece no
# "error log" da aplicação web.

LOG_LEVEL = env_str("LOG_LEVEL") or "INFO"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "transcritor": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
