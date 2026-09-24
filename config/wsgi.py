"""Ponto de entrada WSGI (usado pelo PythonAnywhere e outros servidores)."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
