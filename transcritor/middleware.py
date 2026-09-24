from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse

# Folga para os cabeçalhos do multipart (o limite real é validado no formulário).
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class UploadSizeLimitMiddleware:
    """Recusa uploads grandes demais antes que o corpo da requisição seja lido.

    Precisa ficar antes do CsrfViewMiddleware, que lê o corpo do POST.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.upload_path = None

    def __call__(self, request):
        if self.upload_path is None:
            self.upload_path = reverse("transcritor:upload")
        if request.method == "POST" and request.path == self.upload_path:
            try:
                content_length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                content_length = 0
            if content_length > settings.MAX_UPLOAD_SIZE_BYTES + MULTIPART_OVERHEAD_BYTES:
                return JsonResponse(
                    {"error": f"O arquivo excede o limite de {settings.MAX_UPLOAD_SIZE_MB} MB."},
                    status=413,
                )
        return self.get_response(request)
