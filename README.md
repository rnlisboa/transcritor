# Transcritor da Gabi

Aplicação Django de página única que transforma vídeos em texto:

1. você escolhe um ou vários vídeos;
2. a página envia e processa **um vídeo de cada vez**: extrai o áudio com **FFmpeg** e transcreve com **Vosk**;
3. o texto aparece no card do vídeo enquanto é gerado, e no final surge um botão para copiar.

Cada card também tem um botão **Baixar áudio do vídeo** (arquivo `.m4a`), útil se a transcrição não ficou boa e você quiser usar outro serviço.

Stack: Python 3.10+, Django 5.2, SQLite, templates Django, HTML/CSS/JavaScript puro.
Dependências Python: apenas `Django` e `vosk`. O projeto não precisa de worker, fila, Celery, Redis, Docker nem GPU, e cabe no **plano gratuito do PythonAnywhere**.

> **Sobre a qualidade:** o Vosk é leve (modelo de 51 MB, roda em qualquer CPU), mas o texto sai **sem pontuação, tudo em minúsculas e com alguns erros**. A ideia é copiar o resultado e pedir a uma IA (ChatGPT, Claude, Gemini...) para pontuar e corrigir.

---

## Como funciona

Sem processos em segundo plano: **o navegador conduz o processamento** com várias requisições curtas.

```text
Para cada vídeo, em ordem:
  1. POST /api/videos/upload/                → salva o vídeo e gera a miniatura (PENDING)
  2. POST /api/videos/<id>/extract/          → extrai o áudio (WAV) e APAGA o vídeo (TRANSCRIBING)
  3. POST /api/videos/<id>/transcribe/  (×N) → transcreve ~10 s de processamento por vez,
                                               parando numa pausa da fala, até COMPLETED
```

- **Requisições curtas:** cada pedaço leva ~10 s e para numa pausa da fala, para não cortar palavras. O PythonAnywhere encerra requisições com mais de 5 minutos, e aqui nenhuma chega perto disso.
- **Progresso real:** o percentual indica quanto do áudio já foi transcrito. Durante a extração do áudio não há métrica confiável, então aparece só um indicador animado.
- **Retomável:** o avanço fica salvo no banco. Se a página for fechada, o vídeo aparece como **Pausado**; é só clicar em **Transcrever vídeos** para continuar de onde parou.
- **Pouco disco:** o vídeo é apagado logo após a extração do áudio. O áudio temporário para o Vosk (WAV, ~1,9 MB por minuto) é apagado ao terminar. Ficam a miniatura, o texto e uma cópia compacta do áudio para download (M4A mono 64 kbps, ~0,5 MB por minuto), gerada na mesma chamada do FFmpeg.
- **Áudio para download:** fica disponível assim que o áudio é extraído, inclusive se a transcrição falhar depois. Ele só é apagado ao clicar em **Limpar vídeos**, então limpe de vez em quando para não ocupar a cota de disco.
- **Por que não há worker:** no plano gratuito do PythonAnywhere não existem Always-on Tasks nem tarefas agendadas, e processos em console têm cota de 100 s de CPU por dia. Requisições web não entram nessa cota.
- **Sessão:** não há login. Cada navegador vê e limpa apenas os próprios vídeos, identificados por um cookie de sessão.

**A página precisa ficar aberta** enquanto os vídeos são processados. Se você tentar sair no meio, o navegador pede confirmação.

---

## Requisitos

- **Python 3.10 ou superior**
- **FFmpeg** (executável `ffmpeg`)
- **Modelo Vosk em português** (baixado com um comando, veja abaixo)

### FFmpeg

Para verificar se está instalado:

```bash
ffmpeg -version
```

**Windows:** `winget install Gyan.FFmpeg`. Depois, feche e abra o terminal. Outra opção é baixar o build em https://www.gyan.dev/ffmpeg/builds/ e adicionar a pasta `bin` ao `PATH`.

**Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`

**PythonAnywhere:** já vem instalado.

Se o executável estiver fora do PATH, informe o caminho no `.env`: `FFMPEG_BINARY=C:\ffmpeg\bin\ffmpeg.exe`.

---

## Instalação local

```bash
python -m venv venv
```

Ative o ambiente virtual:

```bash
# Windows (PowerShell)
venv\Scripts\Activate.ps1
# Linux / macOS
source venv/bin/activate
```

Instale as dependências, crie a configuração e baixe o modelo:

```bash
pip install -r requirements.txt
copy .env.example .env      # Windows (no Linux: cp .env.example .env)
python manage.py download_vosk_model
python manage.py migrate
python manage.py runserver
```

Acesse http://127.0.0.1:8000. Não há mais nada para iniciar.

O `.env.example` vem com `DEBUG=True`, pronto para desenvolvimento. Com `DEBUG=False`, a aplicação exige uma `SECRET_KEY` própria.

### Testes

```bash
python manage.py test transcritor
```

Se o FFmpeg e o modelo estiverem instalados, os testes também rodam o fluxo completo de verdade.

---

## Vosk (transcrição)

- O modelo padrão é o **`vosk-model-small-pt-0.3`** (31 MB compactado, 51 MB descompactado, licença Apache 2.0). Ele é carregado **uma vez por processo** do servidor e reaproveitado; o primeiro pedaço após reiniciar o servidor demora 1 a 2 s a mais.
- `python manage.py download_vosk_model` baixa o modelo do site oficial para `models/vosk-model-small-pt-0.3`.
- Com um `.zip` baixado manualmente, use `python manage.py download_vosk_model --zip caminho/do/arquivo.zip`.
- Existe um modelo grande em português (`vosk-model-pt-fb-v0.1.1-20220516_2113`, 1,6 GB, licença GPLv3), um pouco mais preciso. Ele **não cabe** no plano gratuito do PythonAnywhere. Para usá-lo em outro servidor, aponte `VOSK_MODEL_PATH` para a pasta dele.
- Modelos disponíveis: https://alphacephei.com/vosk/models

---

## Variáveis de ambiente

Elas podem ser definidas no ambiente do sistema ou no arquivo `.env` na raiz do projeto. As do sistema têm prioridade.

| Variável | Padrão | Descrição |
|---|---|---|
| `DEBUG` | `False` | Modo de desenvolvimento. **Nunca use `True` em produção.** |
| `SECRET_KEY` | — | Chave secreta do Django. Obrigatória quando `DEBUG=False`. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Domínios aceitos, separados por vírgula. |
| `CSRF_TRUSTED_ORIGINS` | vazio | Origens HTTPS confiáveis (ex.: `https://usuario.pythonanywhere.com`). |
| `VOSK_MODEL_PATH` | `models/vosk-model-small-pt-0.3` | Pasta do modelo Vosk descompactado. |
| `TRANSCRIBE_CHUNK_SECONDS` | `10` | Segundos de processamento por requisição; cada pedaço termina numa pausa da fala. |
| `MAX_UPLOAD_SIZE_MB` | `200` | Tamanho máximo de cada vídeo. |
| `FFMPEG_BINARY` | `ffmpeg` | Nome ou caminho do executável do FFmpeg. |
| `SECURE_COOKIES` | `True` se `DEBUG=False` | Envia os cookies apenas por HTTPS. |
| `LOG_LEVEL` | `INFO` | Nível de log da aplicação. |
| `MEDIA_ROOT` | `<projeto>/media` | Pasta onde vídeos, áudios (temporários e para download) e miniaturas são salvos. |

Para gerar uma `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

---

## Deploy no PythonAnywhere (plano gratuito)

Nos exemplos, troque `USUARIO` pelo seu nome de usuário.

**Uso de disco:** ~100 MB de dependências + 51 MB do modelo. Sobram cerca de 350 MB dos 512 MB do plano gratuito, usados só temporariamente pelo vídeo que está sendo processado.

### Caminho rápido (pacote .zip + script)

1. No seu computador, gere o pacote com `python deploy/make_zip.py`. Ele cria `dist/transcritor.zip` já com o modelo Vosk (baixe-o antes com `python manage.py download_vosk_model`).
2. **Files:** envie `transcritor.zip` para `/home/USUARIO/`.
3. **Web → Add a new web app → Manual configuration →** escolha o Python (ex.: 3.12).
4. **Bash console:**
   ```bash
   unzip -o ~/transcritor.zip -d ~ && bash ~/transcritor/deploy/pythonanywhere.sh 3.12
   ```
   O script cria o ambiente virtual, instala as dependências, gera o `.env` com uma `SECRET_KEY` nova, prepara o banco e os estáticos e escreve o arquivo WSGI.
5. **Web:** preencha o *Virtualenv* e o *Static files* que o script mostrar no final, ative *Force HTTPS* e clique em **Reload**.

Para atualizar depois, envie o novo `.zip` e repita o passo 4. O `.env` e o banco são mantidos.

Os passos abaixo descrevem a mesma instalação feita manualmente.

### 1. Conta e código

Crie a conta gratuita ("Beginner") em https://www.pythonanywhere.com. Abra um **Bash console** e envie o código. O GitHub está liberado no plano gratuito:

```bash
cd ~
git clone https://github.com/<seu-usuario>/<seu-repositorio>.git transcritor
cd transcritor
```

Se preferir, envie os arquivos pela aba **Files**.

### 2. Ambiente virtual e dependências

```bash
mkvirtualenv --python=/usr/bin/python3.12 transcritor-venv
pip install -r requirements.txt
```

Qualquer versão a partir da 3.10 serve. Para reativar o ambiente depois: `workon transcritor-venv`.

### 3. Modelo Vosk (envio manual)

O site do Vosk (`alphacephei.com`) **não está liberado** no plano gratuito, então o download direto falha. Faça assim:

1. No seu computador, baixe https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip (31 MB).
2. Na aba **Files** do PythonAnywhere, envie o `.zip` para `/home/USUARIO/`.
3. No Bash console:
   ```bash
   cd ~/transcritor
   python manage.py download_vosk_model --zip ~/vosk-model-small-pt-0.3.zip
   rm ~/vosk-model-small-pt-0.3.zip
   ```

### 4. Configuração (`.env`)

```bash
cp .env.example .env
nano .env
```

Ajuste pelo menos:

```env
DEBUG=False
SECRET_KEY=<gere uma chave, veja acima>
ALLOWED_HOSTS=USUARIO.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://USUARIO.pythonanywhere.com
```

### 5. Banco de dados e arquivos estáticos

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check --deploy
```

O banco SQLite fica em `~/transcritor/db.sqlite3`, e o `MEDIA_ROOT` em `~/transcritor/media/`.

### 6. Web App

Na aba **Web**, clique em **Add a new web app** → **Manual configuration** (não escolha "Django") → mesma versão de Python do passo 2.

Na página do Web App:

- **Virtualenv:** `/home/USUARIO/.virtualenvs/transcritor-venv`
- **Source code:** `/home/USUARIO/transcritor`
- **Force HTTPS:** ativado
- **Static files:** URL `/static/` → Directory `/home/USUARIO/transcritor/staticfiles`
  (**não** mapeie `/media/`: os arquivos não devem ser públicos, e miniaturas e áudios passam por views que conferem o dono)

### 7. Arquivo WSGI

Clique no link do **WSGI configuration file**, apague o conteúdo e use:

```python
import os
import sys

path = "/home/USUARIO/transcritor"
if path not in sys.path:
    sys.path.insert(0, path)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
```

O `settings.py` lê o `.env` sozinho, então não é preciso repetir as variáveis aqui.

### 8. Testar

Clique em **Reload**, abra `https://USUARIO.pythonanywhere.com`, escolha um vídeo curto e clique em **Transcrever vídeos**. O card deve passar por *Enviando → Extraindo áudio → Transcrevendo (x%) → Transcrição concluída*.

### 9. Logs

| Onde | O que aparece |
|---|---|
| Web → **Error log** | Logs da aplicação: vídeos recebidos, erros do FFmpeg/Vosk, limpezas. |
| Web → **Server log** | Inicialização e reinícios do servidor. |
| Web → **Access log** | Requisições HTTP. |

Para ver mais detalhes, defina `LOG_LEVEL=DEBUG` no `.env` e clique em **Reload**.

### Manutenção

- **Plano gratuito:** o PythonAnywhere desativa o Web App se ele não for renovado periodicamente. Entre na aba **Web** e clique no botão de estender a validade quando o aviso aparecer.
- **Atualizar a aplicação:**
  ```bash
  cd ~/transcritor && git pull
  workon transcritor-venv
  pip install -r requirements.txt
  python manage.py migrate
  python manage.py collectstatic --noinput
  ```
  Depois, clique em **Reload**.

---

## Estrutura

```text
├── manage.py
├── requirements.txt
├── .env.example
├── config/                      # settings, urls, wsgi
├── transcritor/
│   ├── models.py                # Video (status, arquivos, posição no áudio, transcrição)
│   ├── views.py                 # página + API JSON (upload, extrair, transcrever, limpar, miniatura, áudio)
│   ├── forms.py                 # validação do upload (extensão, tamanho, MIME, assinatura do arquivo)
│   ├── middleware.py            # recusa uploads grandes antes de ler o corpo
│   ├── services/
│   │   ├── ffmpeg.py            # extração de áudio (WAV + M4A) e miniatura (subprocess, sem shell)
│   │   ├── transcription.py     # Vosk: modelo carregado uma vez, transcrição em pedaços
│   │   └── processor.py         # etapas do processamento e limpeza
│   ├── management/commands/download_vosk_model.py
│   └── tests.py
├── templates/                   # index.html, 404.html, 500.html
├── static/                      # css/style.css, js/app.js, img/
├── models/                      # modelo Vosk (não versionado)
└── media/                       # videos/, audio/ (WAV temporário + M4A para download), thumbnails/
```

## Segurança

- CSRF em todos os POSTs; cada etapa só age sobre vídeos da própria sessão.
- Upload validado por extensão, tamanho, MIME type e **assinatura binária do arquivo** (MP4/MOV, WebM/MKV, AVI).
- O nome enviado pelo usuário serve só para exibição. No disco, o arquivo recebe um nome aleatório (UUID), o que impede path traversal.
- O FFmpeg é chamado com lista de argumentos, sem `shell=True`.
- Com `DEBUG=False`, nenhuma stack trace chega ao navegador. As mensagens para o usuário são amigáveis, e os detalhes técnicos vão para os logs.
- Duas abas processando o mesmo vídeo não duplicam texto: cada pedaço só é salvo se a posição no áudio não mudou no meio tempo.

## Limitações conhecidas

- **Qualidade do Vosk:** texto sem pontuação e com erros. Revise com outra IA.
- **A página precisa ficar aberta** durante o processamento. Se fechar, é só retomar depois.
- No plano gratuito, o servidor tem poucos processos web. Enquanto um pedaço é transcrito (~10 s), outras requisições podem esperar. É suficiente para poucas pessoas.
- A velocidade depende da CPU do servidor. Na máquina de testes, cada minuto de áudio levou ~20 s. No PythonAnywhere tende a ser mais lento.
- Uploads grandes dependem também dos limites do servidor. Vídeos muito grandes podem ser recusados antes de chegar ao Django.
- A miniatura exibida antes do envio é gerada pelo navegador. Formatos que ele não reproduz (ex.: AVI) mostram um placeholder até o servidor gerar a miniatura.
