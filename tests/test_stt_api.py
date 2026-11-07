import io
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import MAX_STT_AUDIO_BYTES, create_app
from voice import stt_service


def wav_bytes(seconds: float, sample_rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    frames = round(seconds * sample_rate)
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\0\0" * frames)
    return output.getvalue()


class SttApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = self.app.test_client()

    def post_audio(self, content=b"audio", language=None, filename="sample.wav"):
        data = {"audio": (io.BytesIO(content), filename)}
        if language is not None:
            data["language"] = language
        return self.client.post("/voice/transcribe", data=data)

    @patch("voice.stt_service.transcribe_audio")
    def test_success_for_auto_korean_and_english(self, transcribe) -> None:
        for language, detected, text in (
                (None, "ko", "안녕하세요"), ("ko", "ko", "테스트"),
                ("en", "en", "hello")):
            with self.subTest(language=language):
                transcribe.return_value = stt_service.Transcription(text, detected, 1.0)
                response = self.post_audio(language=language)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["text"], text)
                self.assertEqual(response.json["language"], detected)
                self.assertGreaterEqual(response.json["elapsed_seconds"], 0)
                self.assertEqual(transcribe.call_args.args[1], language or "auto")

    @patch("voice.stt_service.transcribe_audio")
    def test_missing_empty_and_invalid_language(self, transcribe) -> None:
        missing = self.client.post("/voice/transcribe", data={"language": "auto"})
        empty = self.post_audio(b"")
        invalid = self.post_audio(language="fr")
        self.assertEqual((missing.status_code, missing.json["error"]["code"]),
                         (400, "audio_missing"))
        self.assertEqual((empty.status_code, empty.json["error"]["code"]),
                         (400, "audio_empty"))
        self.assertEqual((invalid.status_code, invalid.json["error"]["code"]),
                         (400, "invalid_stt_language"))
        transcribe.assert_not_called()

    @patch("voice.stt_service.transcribe_audio")
    def test_audio_size_boundary_is_endpoint_specific(self, transcribe) -> None:
        transcribe.return_value = stt_service.Transcription("ok", "en", 1.0)
        with self.app.test_client() as client:
            accepted = client.post("/voice/transcribe", data={
                "audio": (io.BytesIO(b"x" * MAX_STT_AUDIO_BYTES), "sample.wav"),
            })
            self.assertEqual(accepted.status_code, 200)
            accepted.request.environ["wsgi.input"].close()
            accepted.close()
        with self.app.test_client() as client:
            rejected = client.post("/voice/transcribe", data={
                "audio": (io.BytesIO(b"x" * (MAX_STT_AUDIO_BYTES + 1)), "sample.wav"),
            })
            self.assertEqual(rejected.status_code, 413)
            self.assertEqual(rejected.json["error"]["code"], "audio_too_large")
            rejected.request.environ["wsgi.input"].close()
            rejected.close()
        with self.app.test_client() as client:
            old_limit = client.post(
                "/chat", data=b"x" * (1024 * 1024 + 1), content_type="application/json",
            )
            self.assertEqual(old_limit.status_code, 413)
            old_limit.close()

    def test_corrupt_audio_is_rejected_by_decoder(self) -> None:
        response = self.post_audio(b"not an audio file", filename="fake.wav")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json["error"]["code"], "invalid_audio")

    def test_service_errors_are_sanitized_and_mapped(self) -> None:
        cases = (
            (stt_service.SttBusyError(), 429, "stt_busy"),
            (stt_service.SttAudioTooLongError(), 413, "audio_too_long"),
            (stt_service.SttAudioError(), 415, "invalid_audio"),
            (stt_service.SttModelMissingError(), 503, "stt_model_missing"),
            (stt_service.SttInitializationError(), 503, "stt_initialization_failed"),
            (stt_service.SttInferenceError(), 500, "stt_inference_failed"),
            (stt_service.SttEmptyResultError(), 422, "stt_empty_result"),
        )
        for error, status, code in cases:
            with self.subTest(code=code), patch(
                    "voice.stt_service.transcribe_audio", side_effect=error):
                response = self.post_audio()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json["error"]["code"], code)
                self.assertNotIn("Traceback", response.get_data(as_text=True))

    def test_concurrent_api_request_returns_busy_without_waiting(self) -> None:
        entered = Event()
        release = Event()
        model = MagicMock()

        def delayed_transcription(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release inference")
            return iter([SimpleNamespace(text=" hello")]), SimpleNamespace(language="en")

        model.transcribe.side_effect = delayed_transcription

        def post():
            with self.app.test_client() as client:
                return client.post("/voice/transcribe", data={
                    "audio": (io.BytesIO(b"audio"), "sample.wav"),
                })

        with patch.object(stt_service, "_decode_audio", return_value=([0.0], 1.0)), \
                patch.object(stt_service, "_load_model", return_value=model), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(post).result(timeout=2)
                self.assertEqual(second.status_code, 429)
                self.assertEqual(second.json["error"]["code"], "stt_busy")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)


class SttServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        stt_service._model = None

    def tearDown(self) -> None:
        stt_service._model = None

    def test_browser_pcm_wav_uses_direct_decoder(self) -> None:
        decoded, seconds = stt_service._decode_audio(wav_bytes(1.0, sample_rate=48_000))
        self.assertEqual(len(decoded), 16_000)
        self.assertEqual(seconds, 1.0)

    def test_decoded_duration_boundary(self) -> None:
        decoded, duration = stt_service._decode_audio(wav_bytes(30.0))
        self.assertEqual(len(decoded), 30 * stt_service.SAMPLE_RATE)
        self.assertEqual(duration, 30.0)
        with self.assertRaises(stt_service.SttAudioTooLongError):
            stt_service._decode_audio(wav_bytes(30.0 + 1 / stt_service.SAMPLE_RATE))

    def test_model_is_lazy_loaded_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory)
            for name in stt_service.REQUIRED_MODEL_FILES:
                (model_path / name).touch()
            fake_model = object()
            with patch.object(stt_service, "MODEL_PATH", model_path), \
                    patch("faster_whisper.WhisperModel", return_value=fake_model) as loader:
                self.assertIs(stt_service._load_model(), fake_model)
                self.assertIs(stt_service._load_model(), fake_model)
            loader.assert_called_once_with(
                str(model_path), device="cpu", compute_type="int8", local_files_only=True,
            )

    def test_missing_model_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stt_service, "MODEL_PATH", Path(directory)):
            with self.assertRaises(stt_service.SttModelMissingError):
                stt_service._load_model()

    def test_inference_failure_and_empty_result(self) -> None:
        model = MagicMock()
        with patch.object(stt_service, "_decode_audio", return_value=([0.0], 1.0)), \
                patch.object(stt_service, "_load_model", return_value=model):
            model.transcribe.side_effect = RuntimeError("private")
            with self.assertRaises(stt_service.SttInferenceError):
                stt_service.transcribe_audio(b"audio")

            model.transcribe.side_effect = None
            model.transcribe.return_value = (iter([SimpleNamespace(text="   ")]),
                                              SimpleNamespace(language="en"))
            with self.assertRaises(stt_service.SttEmptyResultError):
                stt_service.transcribe_audio(b"audio")

    def test_busy_request_does_not_wait(self) -> None:
        self.assertTrue(stt_service._inference_lock.acquire(blocking=False))
        try:
            with self.assertRaises(stt_service.SttBusyError):
                stt_service.transcribe_audio(b"audio")
        finally:
            stt_service._inference_lock.release()

    def test_success_reuses_memory_without_temp_file(self) -> None:
        model = MagicMock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(text=" hello")]), SimpleNamespace(language="en"),
        )
        with patch.object(stt_service, "_decode_audio", return_value=([0.0], 1.0)), \
                patch.object(stt_service, "_load_model", return_value=model), \
                patch("tempfile.NamedTemporaryFile") as temporary_file:
            result = stt_service.transcribe_audio(b"private audio", "auto")
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.language, "en")
        temporary_file.assert_not_called()
        model.transcribe.assert_called_once_with(
            [0.0], beam_size=1, language=None, vad_filter=False,
        )


if __name__ == "__main__":
    unittest.main()
