#!/usr/bin/env bash
# Configura o Transcritor da Gabi no PythonAnywhere.
#
# Uso (no Bash console do PythonAnywhere, depois de criar o Web App):
#     bash ~/transcritor/deploy/pythonanywhere.sh 3.12
#
# O número é a versão do Python escolhida ao criar o Web App.
# Pode ser executado de novo sem problemas (ex.: depois de atualizar o código).
set -euo pipefail

PY_VERSION="${1:-3.12}"
APP_DIR="$HOME/transcritor"
VENV="$HOME/.virtualenvs/transcritor-venv"
USERNAME="${USER:-$(whoami)}"
MODEL_NAME="vosk-model-small-pt-0.3"

step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nERRO: %s\n\n' "$1" >&2; exit 1; }

cd "$APP_DIR" 2>/dev/null || fail "Pasta $APP_DIR não encontrada. Descompacte o transcritor.zip na sua pasta home: unzip -o ~/transcritor.zip -d ~"

step "Procurando o Web App"
shopt -s nullglob
WSGI_FILES=(/var/www/"${USERNAME}"_*_wsgi.py)
[ ${#WSGI_FILES[@]} -ge 1 ] || fail "Nenhum Web App encontrado. Crie o Web App na aba Web (Manual configuration) e rode este comando de novo."
WSGI_FILE="${WSGI_FILES[0]}"
DOMAIN="$(basename "$WSGI_FILE" _wsgi.py | tr '_' '.')"
echo "Web App: https://$DOMAIN"

PYTHON="python$PY_VERSION"
command -v "$PYTHON" >/dev/null || fail "$PYTHON não existe neste servidor. Use a mesma versão do Web App (ex.: 3.10, 3.11, 3.12 ou 3.13)."

step "Criando o ambiente virtual ($PYTHON)"
if [ -x "$VENV/bin/python" ]; then
    echo "Ambiente virtual já existe (mantido)."
else
    "$PYTHON" -m venv "$VENV"
fi

step "Instalando as dependências (pode levar alguns minutos)"
# --no-cache-dir: o cache do pip ocuparia espaço da cota de disco.
"$VENV/bin/pip" install --no-cache-dir --quiet --disable-pip-version-check -r requirements.txt
echo "Dependências instaladas."

step "Criando o arquivo de configuração (.env)"
if [ -f .env ]; then
    echo ".env já existe (mantido)."
else
    SECRET="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(50))')"
    cat > .env <<EOF
DEBUG=False
SECRET_KEY=$SECRET
ALLOWED_HOSTS=$DOMAIN
CSRF_TRUSTED_ORIGINS=https://$DOMAIN
MAX_UPLOAD_SIZE_MB=200
TRANSCRIBE_CHUNK_SECONDS=10
FFMPEG_BINARY=ffmpeg
EOF
    chmod 600 .env
    echo ".env criado para $DOMAIN."
fi

step "Verificando o FFmpeg"
if command -v ffmpeg >/dev/null; then
    ffmpeg -version 2>/dev/null | sed -n 1p
else
    echo "AVISO: ffmpeg não encontrado. Na aba Account → System image, escolha a imagem mais recente."
fi

step "Verificando o modelo Vosk"
if [ -d "models/$MODEL_NAME" ]; then
    echo "Modelo encontrado em models/$MODEL_NAME."
elif [ -f "$HOME/$MODEL_NAME.zip" ]; then
    "$VENV/bin/python" manage.py download_vosk_model --zip "$HOME/$MODEL_NAME.zip"
    rm -f "$HOME/$MODEL_NAME.zip"
else
    fail "Modelo Vosk ausente. Envie $MODEL_NAME.zip para /home/$USERNAME/ pela aba Files e rode de novo."
fi

step "Preparando o banco de dados e os arquivos estáticos"
"$VENV/bin/python" manage.py migrate --noinput
# --clear: recopia tudo, sem depender da data dos arquivos (que o unzip pode deixar antiga).
"$VENV/bin/python" manage.py collectstatic --noinput --clear --verbosity 0
"$VENV/bin/python" manage.py check

step "Configurando o arquivo WSGI ($WSGI_FILE)"
cat > "$WSGI_FILE" <<EOF
# Gerado por transcritor/deploy/pythonanywhere.sh
import os
import sys

path = "$APP_DIR"
if path not in sys.path:
    sys.path.insert(0, path)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
EOF
echo "Arquivo WSGI atualizado."

cat <<EOF

================================================================
 Pronto! Falta só configurar a aba "Web" e clicar em Reload:

   Virtualenv ......... $VENV
   Static files ....... URL: /static/
                        Directory: $APP_DIR/staticfiles
   Force HTTPS ........ Enabled

 Depois, clique no botão verde "Reload" e abra:
   https://$DOMAIN
================================================================
EOF
