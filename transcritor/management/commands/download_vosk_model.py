import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

MODELS_URL = "https://alphacephei.com/vosk/models/"


class Command(BaseCommand):
    help = "Baixa e descompacta o modelo Vosk configurado em VOSK_MODEL_PATH."

    def add_arguments(self, parser):
        parser.add_argument("--url", help="URL do .zip do modelo (padrão: site oficial do Vosk).")
        parser.add_argument("--zip", dest="zip_path", help="Usa um .zip já baixado em vez de baixar.")

    def handle(self, *args, url=None, zip_path=None, **options):
        target = Path(settings.VOSK_MODEL_PATH)
        if target.is_dir():
            self.stdout.write(f"O modelo já existe em {target}. Nada a fazer.")
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
            if zip_path:
                archive = Path(zip_path)
            else:
                url = url or f"{MODELS_URL}{target.name}.zip"
                archive = Path(tmp) / "model.zip"
                self.stdout.write(f"Baixando {url} ...")
                try:
                    with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as out:
                        shutil.copyfileobj(response, out)
                except OSError as exc:
                    raise CommandError(
                        f"Não foi possível baixar o modelo ({exc}). Baixe o .zip manualmente e use --zip."
                    ) from exc

            extracted = Path(tmp) / "extracted"
            try:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(extracted)  # extractall ignora caminhos absolutos e ".."
            except (OSError, zipfile.BadZipFile) as exc:
                raise CommandError(f"Arquivo .zip inválido: {exc}") from exc

            # O .zip oficial contém uma única pasta com o nome do modelo.
            entries = list(extracted.iterdir())
            source = entries[0] if len(entries) == 1 and entries[0].is_dir() else extracted
            shutil.move(str(source), str(target))

        self.stdout.write(self.style.SUCCESS(f"Modelo instalado em {target}."))
