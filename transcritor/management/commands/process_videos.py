from django.core.management.base import BaseCommand

from transcritor.services.worker import WorkerAlreadyRunning, run_worker


class Command(BaseCommand):
    help = "Worker que processa (extrai áudio e transcreve) os vídeos pendentes, um por vez."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Processa os vídeos pendentes e encerra quando a fila esvaziar (para tarefas agendadas).",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=None,
            help="Segundos entre consultas quando a fila está vazia (padrão: WORKER_POLL_INTERVAL).",
        )

    def handle(self, *args, **options):
        try:
            run_worker(poll_interval=options["interval"], once=options["once"])
        except WorkerAlreadyRunning:
            self.stderr.write("Já existe um worker em execução. Nada a fazer.")
        except KeyboardInterrupt:
            self.stderr.write("Worker interrompido.")
