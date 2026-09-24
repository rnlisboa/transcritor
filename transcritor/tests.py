import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock, skipUnless

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .forms import detect_video_container, sanitize_filename
from .models import Video
from .services import ffmpeg, processor
from .services.errors import ProcessingError
from .services.ffmpeg import FFmpegNotFoundError
from .services.worker import _process_next, worker_is_online

# Cabeçalho mínimo de um MP4 (caixa "ftyp").
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
TEMP_MEDIA = tempfile.mkdtemp(prefix="transcritor-tests-")
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


def mp4_upload(name="aula.mp4", content=MP4_BYTES, content_type="video/mp4"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class FakeTranscriber:
    def __init__(self, text="Olá, mundo.", error=None, on_call=None):
        self.text, self.error, self.on_call, self.calls = text, error, on_call, 0

    def transcribe(self, audio_path, on_progress=None):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if on_progress:
            on_progress(50)
        if self.error:
            raise self.error
        return self.text


def fake_extract_audio(video_path, audio_path):
    Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
    Path(audio_path).write_bytes(b"RIFF" + b"\x00" * 100)
    return audio_path


@override_settings(MEDIA_ROOT=TEMP_MEDIA, WORKER_STATE_DIR=Path(TEMP_MEDIA) / "var", MAX_UPLOAD_SIZE_MB=1,
                   MAX_UPLOAD_SIZE_BYTES=1024 * 1024)
class BaseTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.client = Client()
        self.client.get(reverse("transcritor:index"))  # cria a sessão (dono dos vídeos)
        self.owner_key = self.client.session["owner_key"]

    def create_video(self, owner_key=None, status=Video.Status.PENDING, name="video.mp4"):
        video = Video(owner_key=owner_key or self.owner_key, original_name=name, status=status)
        video.video_file.save(name, ContentFile(MP4_BYTES), save=False)
        video.save()
        return video


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
        with mock.patch("transcritor.services.processor.process_video") as process:
            response = self.upload(mp4_upload())
        self.assertEqual(response.status_code, 201)
        process.assert_not_called()  # a transcrição nunca acontece na requisição
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


class StatusEndpointTests(BaseTestCase):
    def test_returns_only_own_videos_with_queue_position(self):
        self.create_video(owner_key="outra-pessoa", status=Video.Status.TRANSCRIBING)
        mine = self.create_video()
        data = self.client.get(reverse("transcritor:status")).json()
        self.assertEqual([v["id"] for v in data["videos"]], [mine.pk])
        self.assertEqual(data["videos"][0]["status"], "PENDING")
        self.assertEqual(data["videos"][0]["queue_position"], 1)  # um vídeo sendo processado à frente
        self.assertIn("worker_online", data)

    def test_transcription_endpoint(self):
        video = self.create_video(status=Video.Status.COMPLETED)
        Video.objects.filter(pk=video.pk).update(transcription="Texto final.")
        status = self.client.get(reverse("transcritor:status")).json()["videos"][0]
        self.assertTrue(status["has_transcription"])
        data = self.client.get(reverse("transcritor:transcription", args=[video.pk])).json()
        self.assertEqual(data["transcription"], "Texto final.")

    def test_cannot_read_other_owner_transcription(self):
        video = self.create_video(owner_key="outra-pessoa", status=Video.Status.COMPLETED)
        response = self.client.get(reverse("transcritor:transcription", args=[video.pk]))
        self.assertEqual(response.status_code, 404)

    def test_index_renders_title(self):
        response = self.client.get(reverse("transcritor:index"))
        self.assertContains(response, "Transcritor da Gabi")
        self.assertContains(response, "Escolher vídeos para transcrição")


class ClearTests(BaseTestCase):
    def test_clear_removes_records_and_files_of_owner_only(self):
        mine = self.create_video()
        thumb = Path(TEMP_MEDIA) / "thumbnails" / "t.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"jpg")
        Video.objects.filter(pk=mine.pk).update(thumbnail="thumbnails/t.jpg")
        other = self.create_video(owner_key="outra-pessoa")

        response = self.client.post(reverse("transcritor:clear"))

        self.assertEqual(response.json()["removed"], 1)
        self.assertFalse(Video.objects.filter(pk=mine.pk).exists())
        self.assertFalse(Path(mine.video_file.path).exists())
        self.assertFalse(thumb.exists())
        self.assertTrue(Video.objects.filter(pk=other.pk).exists())
        self.assertTrue(Path(other.video_file.path).exists())

    def test_clear_tolerates_missing_files(self):
        video = self.create_video()
        Path(video.video_file.path).unlink()
        response = self.client.post(reverse("transcritor:clear"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Video.objects.exists())


@mock.patch("transcritor.services.processor.ffmpeg.extract_audio", side_effect=fake_extract_audio)
class ProcessorTests(BaseTestCase):
    def test_claim_is_atomic_and_in_order(self, _extract):
        first, second = self.create_video(), self.create_video()
        claimed = processor.claim_next_video()
        self.assertEqual(claimed.pk, first.pk)
        self.assertEqual(claimed.status, Video.Status.EXTRACTING_AUDIO)
        self.assertEqual(processor.claim_next_video().pk, second.pk)
        self.assertIsNone(processor.claim_next_video())

    def test_successful_processing(self, _extract):
        video = self.create_video()
        processor.process_video(processor.claim_next_video(), FakeTranscriber("Bom dia."))
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.COMPLETED)
        self.assertEqual(video.transcription, "Bom dia.")
        self.assertEqual(video.progress, 100)
        self.assertEqual(video.audio_file.name, "")
        self.assertFalse(any((Path(TEMP_MEDIA) / "audio").glob("*.wav")))  # áudio temporário removido

    def test_status_changes_to_transcribing_during_transcription(self, _extract):
        video = self.create_video()
        seen = []
        transcriber = FakeTranscriber(on_call=lambda: seen.append(Video.objects.get(pk=video.pk).status))
        processor.process_video(processor.claim_next_video(), transcriber)
        self.assertEqual(seen, [Video.Status.TRANSCRIBING])

    def test_ffmpeg_error_marks_video_as_error_with_friendly_message(self, extract):
        extract.side_effect = ProcessingError("O vídeo não possui trilha de áudio para transcrever.")
        video = self.create_video()
        processor.process_video(processor.claim_next_video(), FakeTranscriber())
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.ERROR)
        self.assertEqual(video.error_message, "O vídeo não possui trilha de áudio para transcrever.")

    def test_unexpected_error_does_not_leak_details(self, _extract):
        video = self.create_video()
        processor.process_video(processor.claim_next_video(), FakeTranscriber(error=RuntimeError("segredo interno")))
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.ERROR)
        self.assertNotIn("segredo", video.error_message)

    def test_worker_continues_after_error(self, _extract):
        failing, ok = self.create_video(), self.create_video()
        transcriber = FakeTranscriber()
        transcriber.error = RuntimeError("falha")
        stop = mock.Mock(is_set=lambda: False)
        self.assertTrue(_process_next(transcriber, stop))
        transcriber.error = None
        self.assertTrue(_process_next(transcriber, stop))
        self.assertFalse(_process_next(transcriber, stop))
        self.assertEqual(Video.objects.get(pk=failing.pk).status, Video.Status.ERROR)
        self.assertEqual(Video.objects.get(pk=ok.pk).status, Video.Status.COMPLETED)

    def test_video_deleted_during_processing_is_not_recreated(self, _extract):
        video = self.create_video()
        transcriber = FakeTranscriber(on_call=lambda: Video.objects.filter(pk=video.pk).delete())
        processor.process_video(processor.claim_next_video(), transcriber)
        self.assertFalse(Video.objects.filter(pk=video.pk).exists())
        self.assertFalse(Path(video.video_file.path).exists())

    def test_stop_request_requeues_video(self, _extract):
        video = self.create_video()
        processor.process_video(processor.claim_next_video(), FakeTranscriber(), should_stop=lambda: True)
        self.assertEqual(Video.objects.get(pk=video.pk).status, Video.Status.PENDING)

    def test_requeue_interrupted_videos(self, _extract):
        video = self.create_video(status=Video.Status.TRANSCRIBING)
        self.assertEqual(processor.requeue_interrupted_videos(), 1)
        self.assertEqual(Video.objects.get(pk=video.pk).status, Video.Status.PENDING)

    def test_worker_offline_without_heartbeat(self, _extract):
        self.assertFalse(worker_is_online())


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


@skipUnless(FFMPEG_AVAILABLE, "FFmpeg não instalado")
class FFmpegIntegrationTests(TestCase):
    """Usa o FFmpeg real com um vídeo sintético de 2 segundos."""

    def test_thumbnail_and_audio_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            video = tmp / "teste.mp4"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-pix_fmt", "yuv420p", str(video)],
                check=True,
            )
            self.assertTrue(ffmpeg.generate_thumbnail(video, tmp / "thumb.jpg"))
            audio = ffmpeg.extract_audio(video, tmp / "audio.wav")
            self.assertGreater(audio.stat().st_size, 44)

    def test_video_without_audio_has_friendly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "mudo.mp4"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=5",
                 "-pix_fmt", "yuv420p", str(video)],
                check=True,
            )
            with self.assertRaises(ProcessingError) as ctx:
                ffmpeg.extract_audio(video, Path(tmp) / "a.wav")
            self.assertIn("não possui trilha de áudio", ctx.exception.user_message)
