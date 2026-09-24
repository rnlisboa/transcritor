"""Gera dist/transcritor.zip para enviar ao PythonAnywhere.

Uso: python deploy/make_zip.py
(o modelo Vosk precisa estar em models/: python manage.py download_vosk_model)
"""

import time
import zipfile
from pathlib import Path

project = Path(__file__).resolve().parent.parent
output = project / "dist" / "transcritor.zip"
output.parent.mkdir(exist_ok=True)

if not any((project / "models").glob("vosk-model-*")):
    raise SystemExit("Modelo Vosk não encontrado em models/. Rode antes: python manage.py download_vosk_model")

INCLUDE_DIRS = ["config", "transcritor", "templates", "static", "models", "deploy"]
INCLUDE_FILES = ["manage.py", "requirements.txt", "README.md", ".env.example", ".gitignore"]
MEDIA_KEEP = ["media/videos/.gitkeep", "media/audio/.gitkeep", "media/thumbnails/.gitkeep"]

# Data de agora em todos os arquivos: ao descompactar, eles ficam mais novos que os já instalados.
now = time.localtime()[:6]
count = 0
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    paths = [project / f for f in INCLUDE_FILES + MEDIA_KEEP]
    for folder in INCLUDE_DIRS:
        paths += [p for p in (project / folder).rglob("*") if p.is_file()]
    for path in paths:
        rel = path.relative_to(project)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        if path.suffix == ".sh":
            data = data.replace(b"\r\n", b"\n")  # scripts precisam de fim de linha Unix
        info = zipfile.ZipInfo(("transcritor" / rel).as_posix(), date_time=now)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
        zf.writestr(info, data)
        count += 1

print(f"{output}  ({count} arquivos, {output.stat().st_size / 1024 / 1024:.1f} MB)")
