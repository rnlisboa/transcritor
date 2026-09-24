class ProcessingError(Exception):
    """Falha esperada no processamento, com uma mensagem amigável para o usuário.

    Os detalhes técnicos devem ir para o log; `user_message` vai para a interface.
    """

    default_message = "Não foi possível processar o vídeo."

    def __init__(self, user_message=None):
        self.user_message = user_message or self.default_message
        super().__init__(self.user_message)


class ProcessingAborted(Exception):
    """Interrupção controlada do processamento (não é um erro do vídeo)."""


class VideoDeletedError(ProcessingAborted):
    """O vídeo foi removido (ex.: "Limpar vídeos") enquanto era processado."""


class WorkerStopRequested(ProcessingAborted):
    """O worker recebeu um pedido de encerramento; o vídeo volta para a fila."""
