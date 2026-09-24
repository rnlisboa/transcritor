import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock, skipUnless

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .forms import detect_video_container, sanitize_filename
from .models import Video
from .services import ffmpeg, processor
from .services.errors import ProcessingError
from .services.ffmpeg import FFmpegNotFoundError
from .services.transcription import ChunkResult

# Cabeçalho mínimo de um MP4 (caixa "ftyp").
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
TEMP_MEDIA = tempfile.mkdtemp(prefix="transcritor-tests-")
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
VOSK_MODEL_AVAILABLE = Path(settings.VOSK_MODEL_PATH).is_dir()


def mp4_upload(name="aula.mp4", content=MP4_BYTES, content_type="video/mp4"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def fake_extract_audio(video_path, audio_path, download_path=None):
    Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
    Path(audio_path).write_bytes(b"RIFF" + b"\x00" * 100)
    if download_path is not None:
        Path(download_path).write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 50)
    return audio_path


@override_settings(MEDIA_ROOT=TEMP_MEDIA, MAX_UPLOAD_SIZE_MB=1, MAX_UPLOAD_SIZE_BYTES=1024 * 1024)
class BaseTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.client = Client()
        self.client.get(reverse("transcritor:index"))  # cria a sessão (dona dos vídeos)
        self.owner_key = self.client.session["owner_key"]

    def create_video(self, owner_key=None, status=Video.Status.PENDING, name="video.mp4"):
        video = Video(owner_key=owner_key or self.owner_key, original_name=name, status=status)
        video.video_file.save(name, ContentFile(MP4_BYTES), save=False)
        video.save()
        return video

    def create_transcribing_video(self, **kwargs):
        video = self.create_video(status=Video.Status.TRANSCRIBING, **kwargs)
        audio_name = processor.audio_name_for(video)
        download_name = processor.download_audio_name_for(video)
        fake_extract_audio(None, Path(TEMP_MEDIA) / audio_name, Path(TEMP_MEDIA) / download_name)
        Video.objects.filter(pk=video.pk).update(audio_file=audio_name, download_audio=download_name, video_file="")
        video.refresh_from_db()
        return video

    def post_step(self, name, video):
        return self.client.post(reverse(f"transcritor:{name}", args=[video.pk]))


class VideoModelTests(BaseTestCase):
    def test_new_video_starts_pending_and_file_gets_random_name(self):
        video = self.create_video(name="../../etc/minha aula.MP4")
        self.assertEqual(video.status, Video.Status.PENDING)
        self.assertRegex(video.video_file.name, r"^videos/[0-9a-f]{32}\.mp4$")


class ValidationTests(TestCase):
    def test_sanitize_filename_removes_directories_and_control_chars(self):
        self.assertEqual(sanitize_filename("..\\..\\pasta/aula\x00 1.mp4"), "aula 1.mp4")

    def test_detect_video_container(self):
        self.assertEqual(detect_video_container(MP4_BYTES[:16]), "mp4/mov")
        self.assertEqual(detect_video_container(b"\x1a\x45\xdf\xa3" + b"\x00" * 12), "webm/mkv")
        self.assertEqual(detect_video_container(b"RIFF\x00\x00\x00\x00AVI LIST"), "avi")
        self.assertIsNone(detect_video_container(b"%PDF-1.7 qualquer"))


@mock.patch("transcritor.views.ffmpeg.generate_thumbnail", return_value=False)
class UploadTests(BaseTestCase):
    def upload(self, file):
        return self.client.post(reverse("transcritor:upload"), {"file": file})

    def test_upload_creates_pending_video_without_processing(self, _thumb):
        with mock.patch("transcritor.services.processor.extract_audio") as extract:
            response = self.upload(mp4_upload())
        self.assertEqual(response.status_code, 201)
        extract.assert_not_called()  # o upload só salva; as etapas vêm depois
        data = response.json()["video"]
        self.assertEqual(data["status"], "PENDING")
        self.assertEqual(data["name"], "aula.mp4")
        video = Video.objects.get(pk=data["id"])
        self.assertEqual(video.owner_key, self.owner_key)
        self.assertTrue(Path(video.video_file.path).is_file())

    def test_rejects_disallowed_extension(self, _thumb):
        response = self.upload(mp4_upload(name="script.exe", content_type="application/octet-stream"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Formato não suportado", response.json()["error"])
        self.assertFalse(Video.objects.exists())

    def test_rejects_fake_video_content(self, _thumb):
        response = self.upload(mp4_upload(content=b"isto nao e um video" * 10))
        self.assertEqual(response.status_code, 400)
        self.assertIn("não corresponde a um vídeo", response.json()["error"])

    def test_rejects_non_video_mime_type(self, _thumb):
        response = self.upload(mp4_upload(content_type="text/html"))
        self.assertEqual(response.status_code, 400)

    def test_rejects_file_above_max_size(self, _thumb):
        big = MP4_BYTES + b"\x00" * (1024 * 1024)
        response = self.upload(mp4_upload(content=big))
        self.assertEqual(response.status_code, 400)
        self.assertIn("limite de 1 MB", response.json()["error"])

    def test_middleware_rejects_huge_request_before_reading_body(self, _thumb):
        response = self.client.post(
            reverse("transcritor:upload"), data=b"x", content_type="application/octet-stream",
            CONTENT_LENGTH=str(50 * 1024 * 1024),
        )
        self.assertEqual(response.status_code, 413)

    def test_requires_csrf_token(self, _thumb):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("transcritor:upload"), {"file": mp4_upload()})
        self.assertEqual(response.status_code, 403)

    def test_upload_requires_post(self, _thumb):
        self.assertEqual(self.client.get(reverse("transcritor:upload")).status_code, 405)


class PageTests(BaseTestCase):
    def test_index_renders_title_and_only_own_videos(self):
        self.create_video(name="meu.mp4")
        self.create_video(owner_key="outra-pessoa", name="alheio.mp4")
        response = self.client.get(reverse("transcritor:index"))
        self.assertContains(response, "Transcritor da Gabi")
        self.assertContains(response, "meu.mp4")
        self.assertNotContains(response, "alheio.mp4")


@mock.patch("transcritor.services.processor.ffmpeg.extract_audio", side_effect=fake_extract_audio)
class ExtractStepTests(BaseTestCase):
    def test_extract_moves_to_transcribing_and_deletes_video(self, _extract):
        video = self.create_video()
        response = self.post_step("extract", video)
        self.assertEqual(response.json()["video"]["status"], "TRANSCRIBING")
        video_path = Path(video.video_file.path)
        video.refresh_from_db()
        self.assertEqual(video.video_file.name, "")
        self.assertFalse(video_path.exists())  # o vídeo é apagado para economizar disco
        self.assertTrue(Path(video.audio_file.path).is_file())
        self.assertTrue(Path(video.download_audio.path).is_file())
        self.assertEqual(response.json()["video"]["audio_url"], reverse("transcritor:audio", args=[video.pk]))

    def test_extract_error_removes_download_audio(self, extract):
        def fail_after_writing(video_path, audio_path, download_path=None):
            fake_extract_audio(video_path, audio_path, download_path)
            raise ProcessingError("O arquivo está corrompido ou não é um vídeo válido.")

        extract.side_effect = fail_after_writing
        video = self.create_video()
        data = self.post_step("extract", video).json()["video"]
        self.assertEqual(data["status"], "ERROR")
        self.assertIsNone(data["audio_url"])
        self.assertFalse((Path(TEMP_MEDIA) / processor.download_audio_name_for(video)).exists())

    def test_ffmpeg_error_marks_video_as_error_with_friendly_message(self, extract):
        extract.side_effect = ProcessingError("O vídeo não possui trilha de áudio para transcrever.")
        video = self.create_video()
        data = self.post_step("extract", video).json()["video"]
        self.assertEqual(data["status"], "ERROR")
        self.assertEqual(data["error"], "O vídeo não possui trilha de áudio para transcrever.")
        self.assertFalse(Path(video.video_file.path).exists())

    def test_unexpected_error_does_not_leak_details(self, extract):
        extract.side_effect = RuntimeError("segredo interno")
        data = self.post_step("extract", self.create_video()).json()["video"]
        self.assertEqual(data["status"], "ERROR")
        self.assertNotIn("segredo", data["error"])

    def test_extract_on_completed_video_returns_conflict(self, _extract):
        video = self.create_video(status=Video.Status.COMPLETED)
        response = self.post_step("extract", video)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["video"]["status"], "COMPLETED")

    def test_cannot_process_other_owner_video(self, _extract):
        video = self.create_video(owner_key="outra-pessoa")
        self.assertEqual(self.post_step("extract", video).status_code, 404)

    def test_steps_require_post(self, _extract):
        video = self.create_video()
        self.assertEqual(self.client.get(reverse("transcritor:extract", args=[video.pk])).status_code, 405)


class TranscribeStepTests(BaseTestCase):
    def test_chunks_accumulate_text_and_finish(self):
        video = self.create_transcribing_video()
        chunks = [ChunkResult("olá gabi", 50, 100), ChunkResult("tudo bem", 100, 100)]
        with mock.patch("transcritor.services.processor.transcription.transcribe_chunk", side_effect=chunks) as chunk:
            first = self.post_step("transcribe", video).json()["video"]
            second = self.post_step("transcribe", video).json()["video"]

        self.assertEqual((first["status"], first["progress"]), ("TRANSCRIBING", 50))
        self.assertEqual(first["transcription"], "Olá gabi")
        self.assertEqual(chunk.call_args_list[1].args[1], 50)  # retoma de onde parou
        self.assertEqual((second["status"], second["progress"]), ("COMPLETED", 100))
        self.assertEqual(second["transcription"], "Olá gabi tudo bem")
        self.assertFalse(any((Path(TEMP_MEDIA) / "audio").glob(f"{video.pk}.wav")))  # áudio removido
        self.assertTrue(Path(video.download_audio.path).is_file())  # o áudio para baixar fica
        self.assertIsNotNone(second["audio_url"])

    def test_transcription_error_marks_error(self):
        video = self.create_transcribing_video()
        with mock.patch(
            "transcritor.services.processor.transcription.transcribe_chunk",
            side_effect=ProcessingError("O modelo de transcrição não está disponível no servidor."),
        ):
            data = self.post_step("transcribe", video).json()["video"]
        self.assertEqual(data["status"], "ERROR")
        self.assertIn("modelo", data["error"])
        self.assertIsNotNone(data["audio_url"])  # dá para baixar o áudio e transcrever em outro lugar

    def test_missing_audio_marks_error(self):
        video = self.create_transcribing_video()
        Path(video.audio_file.path).unlink()
        data = self.post_step("transcribe", video).json()["video"]
        self.assertEqual(data["status"], "ERROR")

    def test_concurrent_progress_is_not_overwritten(self):
        video = self.create_transcribing_video()

        def advanced_elsewhere(*args):
            Video.objects.filter(pk=video.pk).update(audio_position=80, transcription="outra aba")
            return ChunkResult("duplicado", 50, 100)

        with mock.patch("transcritor.services.processor.transcription.transcribe_chunk", side_effect=advanced_elsewhere):
            data = self.post_step("transcribe", video).json()["video"]
        self.assertEqual(data["transcription"], "outra aba")

    def test_transcribe_on_pending_video_returns_conflict(self):
        self.assertEqual(self.post_step("transcribe", self.create_video()).status_code, 409)


class AudioDownloadTests(BaseTestCase):
    def test_download_returns_attachment_named_after_video(self):
        video = self.create_transcribing_video(name="Aula 1.mp4")
        response = self.client.get(reverse("transcritor:audio", args=[video.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/mp4")
        self.assertIn('attachment; filename="Aula 1.m4a"', response["Content-Disposition"])
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\x00\x00\x00\x18ftyp"))

    def test_download_without_audio_returns_404(self):
        video = self.create_video()
        self.assertEqual(self.client.get(reverse("transcritor:audio", args=[video.pk])).status_code, 404)

    def test_cannot_download_other_owner_audio(self):
        video = self.create_transcribing_video(owner_key="outra-pessoa")
        self.assertEqual(self.client.get(reverse("transcritor:audio", args=[video.pk])).status_code, 404)


class ClearTests(BaseTestCase):
    def test_clear_removes_records_and_files_of_owner_only(self):
        mine = self.create_video()
        thumb = Path(TEMP_MEDIA) / "thumbnails" / "t.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"jpg")
        Video.objects.filter(pk=mine.pk).update(thumbnail="thumbnails/t.jpg")
        transcribing = self.create_transcribing_video()
        audio = Path(transcribing.audio_file.path)
        download = Path(transcribing.download_audio.path)
        other = self.create_video(owner_key="outra-pessoa")

        response = self.client.post(reverse("transcritor:clear"))

        self.assertEqual(response.json()["removed"], 2)
        self.assertFalse(Video.objects.filter(owner_key=self.owner_key).exists())
        self.assertFalse(Path(mine.video_file.path).exists())
        self.assertFalse(thumb.exists())
        self.assertFalse(audio.exists())
        self.assertFalse(download.exists())
        self.assertTrue(Video.objects.filter(pk=other.pk).exists())
        self.assertTrue(Path(other.video_file.path).exists())

    def test_clear_tolerates_missing_files(self):
        video = self.create_video()
        Path(video.video_file.path).unlink()
        response = self.client.post(reverse("transcritor:clear"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Video.objects.exists())


class FFmpegServiceTests(TestCase):
    @override_settings(FFMPEG_BINARY="ffmpeg-que-nao-existe")
    def test_missing_ffmpeg_raises_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(MP4_BYTES)
            with self.assertRaises(FFmpegNotFoundError):
                ffmpeg.extract_audio(video, Path(tmp) / "a.wav")
            self.assertFalse(ffmpeg.generate_thumbnail(video, Path(tmp) / "t.jpg"))

    def test_command_uses_argument_list_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "nome; rm -rf ~.mp4"
            video.write_bytes(MP4_BYTES)
            with mock.patch("transcritor.services.ffmpeg.subprocess.run") as run:
                run.side_effect = subprocess.CalledProcessError(1, "ffmpeg", stderr="Invalid data found")
                with self.assertRaises(ProcessingError) as ctx:
                    ffmpeg.extract_audio(video, Path(tmp) / "a.wav")
            args, kwargs = run.call_args
            self.assertIsInstance(args[0], list)
            self.assertIn(str(video), args[0])
            self.assertNotIn("shell", kwargs)
            self.assertIn("corrompido", ctx.exception.user_message)


def make_test_video(path, with_audio=True, seconds=2):
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", f"testsrc=duration={seconds}:size=320x240:rate=10"]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    subprocess.run([*command, "-pix_fmt", "yuv420p", str(path)], check=True)


@skipUnless(FFMPEG_AVAILABLE, "FFmpeg não instalado")
class FFmpegIntegrationTests(TestCase):
    """Usa o FFmpeg real com vídeos sintéticos."""

    def test_thumbnail_and_audio_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "teste.mp4"
            make_test_video(video)
            self.assertTrue(ffmpeg.generate_thumbnail(video, Path(tmp) / "thumb.jpg"))
            audio = ffmpeg.extract_audio(video, Path(tmp) / "audio.wav", Path(tmp) / "audio.m4a")
            self.assertGreater(audio.stat().st_size, 44)
            self.assertGreater((Path(tmp) / "audio.m4a").stat().st_size, 0)

    def test_video_without_audio_has_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "mudo.mp4"
            make_test_video(video, with_audio=False, seconds=1)
            with self.assertRaises(ProcessingError) as ctx:
                ffmpeg.extract_audio(video, Path(tmp) / "a.wav")
            self.assertIn("não possui trilha de áudio", ctx.exception.user_message)


@skipUnless(FFMPEG_AVAILABLE and VOSK_MODEL_AVAILABLE, "FFmpeg ou modelo Vosk ausente")
class FullFlowIntegrationTests(BaseTestCase):
    """Fluxo completo com FFmpeg e Vosk reais: extrair → transcrever em pedaços → concluir."""

    def test_full_flow(self):
        video = self.create_video()
        make_test_video(Path(video.video_file.path), seconds=3)  # substitui pelo vídeo real

        self.assertEqual(self.post_step("extract", video).json()["video"]["status"], "TRANSCRIBING")
        for _ in range(20):
            data = self.post_step("transcribe", video).json()["video"]
            if data["status"] != "TRANSCRIBING":
                break
        self.assertEqual(data["status"], "COMPLETED")
        self.assertEqual(data["progress"], 100)
