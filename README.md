# Transcritor da Gabi

Aplicação Django de página única que transforma vídeos em texto:

1. você escolhe um ou vários vídeos;
2. o navegador envia os arquivos um por vez;
3. um **worker separado** extrai o áudio com **FFmpeg** e transcreve com **Faster-Whisper**, sempre **um vídeo de cada vez**;
4. a transcrição aparece no card do vídeo, com um botão para copiar.

Stack: Python 3.10+, Django 5.2, SQLite, templates Django, HTML/CSS/JavaScript puro.
Dependências Python: apenas `Django` e `faster-whisper`. O projeto não usa Celery, Redis, WebSockets, Docker nem GPU.

---

## Como funciona

```text
Navegador ──upload (1 arquivo por requisição)──▶ Django ──▶ SQLite (status PENDING)
    ▲                                                          │
    └──── polling a cada 3 s (/api/videos/status/) ◀───────────┤
                                                               ▼
                               python manage.py process_videos (worker)
                               PENDING → EXTRACTING_AUDIO → TRANSCRIBING → COMPLETED
                                                                         ↘ ERROR
```

- **A requisição de upload nunca transcreve.** Ela valida e salva o arquivo, gera a thumbnail (um único frame, é rápido), cria o registro como `PENDING` e responde.
- **O worker** é um processo à parte. Ele pega o vídeo pendente mais antigo com um `UPDATE ... WHERE status='PENDING'` atômico (dois workers não conseguem pegar o mesmo vídeo) e o processa até o fim antes de pegar o próximo.
- **O modelo Whisper é carregado uma única vez**, quando o worker inicia, e reaproveitado para todos os vídeos.
- **O áudio é temporário**: é um WAV mono de 16 kHz, apagado assim que o processamento termina (com sucesso ou erro).
- **Progresso real**: durante a transcrição, o percentual indica a posição do áudio já transcrita. Na extração de áudio não há métrica confiável, então a interface mostra um indicador animado, sem percentual inventado.
- **Worker parado**: o worker grava um sinal de vida (heartbeat) a cada 10 s. Se ele parar, a interface avisa que os vídeos estão na fila, esperando o processador.
- **Sessão**: não há login. Cada navegador vê e limpa apenas os seus próprios vídeos, identificados por um cookie de sessão. A fila do worker, porém, é compartilhada entre todos.

---

## Requisitos

- **Python 3.10 ou superior**
- **FFmpeg** (executável `ffmpeg`)

### FFmpeg

Para verificar se está instalado:

```bash
ffmpeg -version
```

**Windows** (uma das opções):

```bash
winget install Gyan.FFmpeg
```

Depois, feche e abra o terminal. Outra opção é baixar o build em https://www.gyan.dev/ffmpeg/builds/, extrair e adicionar a pasta `bin` ao `PATH`.

**Linux (Debian/Ubuntu):**

```bash
sudo apt update && sudo apt install ffmpeg
```

**Se o `ffmpeg` não estiver no PATH**, informe o caminho completo no `.env`:

```env
FFMPEG_BINARY=C:\ffmpeg\bin\ffmpeg.exe
# ou
FFMPEG_BINARY=/usr/local/bin/ffmpeg
```

Se o FFmpeg não for encontrado, o worker registra isso no log ao iniciar. O vídeo recebe a mensagem "O FFmpeg não foi encontrado no servidor…".

---

## Instalação local

```bash
python -m venv venv
```

Ative o ambiente virtual:

```bash
# Windows (PowerShell)
venv\Scripts\Activate.ps1
# Windows (cmd)
venv\Scripts\activate.bat
# Linux / macOS
source venv/bin/activate
```

Instale as dependências e crie a configuração:

```bash
pip install -r requirements.txt
copy .env.example .env      # Windows
cp .env.example .env        # Linux
```

O `.env.example` vem com `DEBUG=True`, pronto para desenvolvimento. Com `DEBUG=False`, a aplicação exige uma `SECRET_KEY` própria.

Crie o banco e execute:

```bash
python manage.py migrate
python manage.py runserver
```

Acesse http://127.0.0.1:8000.

### Worker local

Em **outro terminal**, com o mesmo ambiente virtual ativo:

```bash
python manage.py process_videos
```

Deixe-o rodando. Ele consulta a fila a cada `WORKER_POLL_INTERVAL` segundos. Para encerrar, use `Ctrl+C`: o vídeo em andamento volta para a fila e será reprocessado na próxima execução.

Opções:

| Opção | Efeito |
|---|---|
| `--once` | Processa todos os vídeos pendentes e encerra (para tarefas agendadas). |
| `--interval N` | Espera N segundos entre consultas quando a fila está vazia. |

Só pode existir **um worker por vez**: se outro já estiver rodando, o comando avisa e encerra. Se o worker cair no meio de um vídeo, o próximo worker a iniciar devolve esse vídeo para a fila.

### Testes

```bash
python manage.py test transcritor
```

---

## Faster-Whisper

- Na **primeira execução**, o worker **baixa o modelo** escolhido do Hugging Face (o `base` tem ~145 MB). Isso pode levar alguns minutos. Depois o modelo fica em cache (`~/.cache/huggingface`) e não é baixado de novo.
- A transcrição roda em **CPU**, o que é **mais lento** que em GPU. Como referência, o modelo `base` em CPU comum leva de alguns segundos a poucos minutos por minuto de áudio, dependendo da máquina. **Vídeos longos podem demorar bastante.**
- **`base` + `cpu` + `int8` é a configuração padrão**, pensada para servidores simples e de baixo custo.

Para trocar, edite o `.env` e **reinicie o worker**:

```env
WHISPER_MODEL=small           # tiny | base | small | medium | large-v3 | turbo ...
WHISPER_DEVICE=cpu            # cpu | cuda (só com GPU NVIDIA e bibliotecas CUDA)
WHISPER_COMPUTE_TYPE=int8     # int8 (CPU) | float16 (GPU) | float32
WHISPER_LANGUAGE=pt           # código do idioma, ou "auto" para detectar
```

Modelos maiores ficam mais precisos, mas também mais lentos e consomem mais memória. Em CPU, `tiny`, `base` e `small` são as opções realistas.

`WHISPER_MODEL` também aceita **o caminho de uma pasta com o modelo já baixado**, útil em servidores sem acesso ao Hugging Face:

```bash
python -c "from faster_whisper import download_model; print(download_model('base', output_dir='modelo-base'))"
```

Depois, envie a pasta `modelo-base` para o servidor e use `WHISPER_MODEL=/caminho/para/modelo-base`.

---

## Variáveis de ambiente

Elas podem ser definidas no ambiente do sistema ou no arquivo `.env` na raiz do projeto. As do sistema têm prioridade.

| Variável | Padrão | Descrição |
|---|---|---|
| `DEBUG` | `False` | Modo de desenvolvimento. **Nunca use `True` em produção.** |
| `SECRET_KEY` | — | Chave secreta do Django. Obrigatória quando `DEBUG=False`. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Domínios aceitos, separados por vírgula. |
| `CSRF_TRUSTED_ORIGINS` | vazio | Origens HTTPS confiáveis (ex.: `https://usuario.pythonanywhere.com`). |
| `WHISPER_MODEL` | `base` | Nome do modelo ou caminho de uma pasta local. |
| `WHISPER_DEVICE` | `cpu` | `cpu` ou `cuda`. |
| `WHISPER_COMPUTE_TYPE` | `int8` | Precisão do modelo (`int8` é a mais leve em CPU). |
| `WHISPER_LANGUAGE` | `pt` | Idioma do áudio, ou `auto`. |
| `MAX_UPLOAD_SIZE_MB` | `500` | Tamanho máximo de cada vídeo. |
| `FFMPEG_BINARY` | `ffmpeg` | Nome ou caminho do executável do FFmpeg. |
| `WORKER_POLL_INTERVAL` | `5` | Segundos entre consultas do worker quando a fila está vazia. |
| `SECURE_COOKIES` | `True` se `DEBUG=False` | Envia os cookies apenas por HTTPS. |
| `LOG_LEVEL` | `INFO` | Nível de log da aplicação. |
| `MEDIA_ROOT` | `<projeto>/media` | Pasta onde vídeos e thumbnails são salvos. |

Para gerar uma `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

---

## Deploy no PythonAnywhere

Nos exemplos abaixo, troque `USUARIO` pelo seu nome de usuário do PythonAnywhere.

### 0. Conta e plano

Crie a conta em https://www.pythonanywhere.com. Pontos importantes de cada plano (confira os limites atuais na página de preços):

- **Always-on Tasks**, que mantêm o worker rodando 24 h, só existem nos **planos pagos**. É o modo recomendado.
- **Plano gratuito**: tem pouco disco (o ambiente virtual com `faster-whisper` ocupa ~350 MB, fora o modelo e os vídeos), cota diária de CPU pequena, acesso à internet restrito a uma lista de sites e tarefas agendadas só uma vez por dia. Funciona para testes com vídeos curtos, mas não para uso contínuo. Veja o passo 10.

### 1. Código

Abra um **Bash console** e envie o código, por git ou pelo upload da aba *Files*:

```bash
cd ~
git clone <URL-do-seu-repositorio> transcritor
cd transcritor
```

### 2. Ambiente virtual

```bash
mkvirtualenv --python=/usr/bin/python3.12 transcritor-venv
```

Qualquer versão a partir da 3.10 serve. O ambiente fica em `~/.virtualenvs/transcritor-venv` e é ativado automaticamente. Para ativá-lo depois: `workon transcritor-venv`.

### 3. Dependências

```bash
cd ~/transcritor
pip install -r requirements.txt
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

### 5. Banco de dados, estáticos e pastas

```bash
python manage.py migrate          # cria o db.sqlite3 e as tabelas
python manage.py collectstatic --noinput
python manage.py check --deploy
```

O banco SQLite fica em `~/transcritor/db.sqlite3`. Os vídeos ficam em `~/transcritor/media/`, que é o `MEDIA_ROOT`.

### 6. Web App

Na aba **Web**, clique em **Add a new web app** → **Manual configuration** (não escolha "Django") → mesma versão de Python do passo 2.

Na página do Web App:

- **Virtualenv**: `/home/USUARIO/.virtualenvs/transcritor-venv`
- **Source code**: `/home/USUARIO/transcritor`
- **Force HTTPS**: ativado. Isso resolve os avisos de HTTPS do `check --deploy`.

### 7. Arquivo WSGI

Clique no link do **WSGI configuration file** (algo como `/var/www/USUARIO_pythonanywhere_com_wsgi.py`), apague o conteúdo e use:

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

### 8. Arquivos estáticos e mídia

Na seção **Static files** do Web App:

| URL | Directory |
|---|---|
| `/static/` | `/home/USUARIO/transcritor/staticfiles` |

**Não mapeie `/media/`.** Os vídeos não devem ser públicos. As thumbnails passam por uma view que confere o dono do vídeo.

Clique em **Reload** e abra `https://USUARIO.pythonanywhere.com`.

### 9. FFmpeg

O FFmpeg costuma já estar disponível nos servidores do PythonAnywhere. Confira no Bash console:

```bash
ffmpeg -version
```

Se ele não existir ou estiver em outro caminho, instale um build estático na sua pasta e aponte para ele no `.env`:

```env
FFMPEG_BINARY=/home/USUARIO/bin/ffmpeg
```

Depois, **reinicie o worker**.

### 10. Worker

O worker precisa rodar **fora** da aplicação web: processos iniciados dentro de uma requisição não sobrevivem ao fim dela.

Comando do worker:

```bash
/home/USUARIO/.virtualenvs/transcritor-venv/bin/python /home/USUARIO/transcritor/manage.py process_videos
```

Antes de configurá-lo como tarefa, **rode esse comando uma vez no Bash console**. Assim você baixa o modelo Whisper (na primeira vez) e confirma no log que o FFmpeg e o modelo foram carregados. Depois, encerre com `Ctrl+C`.

**Plano pago (recomendado): Always-on Task**

1. Aba **Tasks** → seção **Always-on tasks** → cole o comando acima → **Create**.
2. O PythonAnywhere mantém o processo rodando e o reinicia se ele cair.
3. **Reiniciar o worker** (necessário depois de mudar o `.env` ou atualizar o código): use o botão de **restart** da tarefa, ou **stop** seguido de **start**.
4. **Logs**: clique no ícone de log da tarefa.

**Sem Always-on Tasks (plano gratuito)**

A arquitetura continua a mesma. Só muda quem inicia o worker:

- **Tarefa agendada**: na aba **Tasks** → **Scheduled tasks**, agende o mesmo comando com `--once`. Ele processa a fila e encerra:
  ```bash
  /home/USUARIO/.virtualenvs/transcritor-venv/bin/python /home/USUARIO/transcritor/manage.py process_videos --once
  ```
  Os vídeos esperam na fila até a próxima execução (uma vez por dia no plano gratuito). Se uma execução encontrar outra ainda rodando, ela apenas encerra.
- **Manual**: rode `python manage.py process_videos --once` num Bash console sempre que houver vídeos na fila.

Enquanto o worker não estiver ativo, a interface mostra o aviso "O processador de vídeos não está ativo no momento" e mantém os vídeos na fila.

**Modelo Whisper no plano gratuito**: se o download do Hugging Face for bloqueado pela lista de sites permitidos, baixe o modelo no seu computador, envie a pasta pela aba *Files* e use `WHISPER_MODEL=/home/USUARIO/modelo-base`. Veja a seção Faster-Whisper.

### 11. Testar

1. Abra o site, escolha um vídeo curto e clique em **Transcrever vídeos**.
2. O card deve passar por *Enviando → Aguardando processamento → Extraindo áudio → Transcrevendo → Transcrição concluída*.
3. Se ficar parado em *Aguardando processamento*, confira se o worker está rodando (passo 10).

### 12. Logs

| Onde | O que aparece |
|---|---|
| Web → **Error log** | Erros e logs da aplicação web (uploads, limpeza, exceções). |
| Web → **Server log** | Inicialização e reinícios do servidor web. |
| Web → **Access log** | Requisições HTTP. |
| Tasks → log da tarefa | Tudo do worker: vídeos processados, erros do FFmpeg/Whisper, tempo gasto. |

Para ver mais detalhes, defina `LOG_LEVEL=DEBUG` no `.env` e reinicie o Web App e o worker.

### Atualizar a aplicação

```bash
cd ~/transcritor && git pull
workon transcritor-venv
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```

Depois, clique em **Reload** na aba Web e **reinicie o worker**.

---

## Estrutura

```text
├── manage.py
├── requirements.txt
├── .env.example
├── config/                      # settings, urls, wsgi
├── transcritor/
│   ├── models.py                # Video (status, arquivos, transcrição)
│   ├── views.py                 # página + API JSON (upload, status, limpar, transcrição, thumbnail)
│   ├── forms.py                 # validação do upload (extensão, tamanho, MIME, assinatura do arquivo)
│   ├── middleware.py            # recusa uploads grandes antes de ler o corpo
│   ├── services/
│   │   ├── ffmpeg.py            # extração de áudio e thumbnail (subprocess, sem shell)
│   │   ├── transcription.py     # Faster-Whisper (modelo carregado uma vez)
│   │   ├── processor.py         # fila, processamento de um vídeo, limpeza
│   │   └── worker.py            # loop do worker, lock de instância única, heartbeat
│   ├── management/commands/process_videos.py
│   └── tests.py
├── templates/                   # index.html, 404.html, 500.html
├── static/                      # css/style.css, js/app.js, img/
└── media/                       # videos/, thumbnails/, audio/ (temporário)
```

## Segurança

- CSRF em todos os POSTs; alterações e exclusões só via POST e só nos vídeos da própria sessão.
- Upload validado por extensão, tamanho, MIME type e **assinatura binária do arquivo** (MP4/MOV, WebM/MKV, AVI).
- O nome enviado pelo usuário serve só para exibição. No disco, o arquivo recebe um nome aleatório (UUID), o que impede path traversal.
- O FFmpeg é chamado com lista de argumentos, sem `shell=True`.
- Com `DEBUG=False`, nenhuma stack trace chega ao navegador. As mensagens para o usuário são amigáveis, e os detalhes técnicos vão para os logs.

## Limitações conhecidas

- A transcrição em CPU é lenta para vídeos longos, e cada vídeo espera o anterior terminar. Isso é intencional.
- SQLite atende bem poucos usuários simultâneos. Não é indicado para alto volume.
- Não há login: quem usa o mesmo navegador (a mesma sessão) vê os mesmos vídeos. Os vídeos ficam guardados até alguém clicar em **Limpar vídeos**.
- O lock de instância única do worker vale para uma máquina. Rode apenas um worker.
- Uploads grandes dependem também dos limites do servidor e do plano de hospedagem.
- A miniatura exibida antes do envio é gerada pelo navegador. Formatos que o navegador não reproduz (ex.: AVI) mostram um placeholder até o servidor gerar a thumbnail.
