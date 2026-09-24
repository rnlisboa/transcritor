class ProcessingError(Exception):
    """Falha esperada no processamento, com uma mensagem amigável para o usuário.

    Os detalhes técnicos devem ir para o log; `user_message` vai para a interface.
    """

    default_message = "Não foi possível processar o vídeo."

    def __init__(self, user_message=None):
        self.user_message = user_message or self.default_message
        super().__init__(self.user_message)


class InvalidStateError(Exception):
    """A etapa pedida não combina com o status atual do vídeo (ex.: já concluído)."""
