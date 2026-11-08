import io
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from app import MAX_TTS_TEXT_LENGTH, create_app
from voice import tts_service
from sqlite_test_support import isolated_database


def wav_bytes(frames: int = 2205) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22_050)
        wav_file.writeframes(b"\1\0" * frames)
    return output.getvalue()


def synthesis(language: str = "ko") -> tts_service.Synthesis:
    audio = wav_bytes()
    return tts_service.Synthesis(audio, language, 22_050, 1, 2, 0.1)


class TtsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        isolated_database(self)
        self.app = create_app()
        self.client = self.app.test_client()

    @patch("voice.tts_service.synthesize_text")
    def test_korean_and_english_return_valid_wav(self, synthesize_mock) -> None:
        for language in ("ko", "en"):
            with self.subTest(language=language):
                synthesize_mock.return_value = synthesis(language)
                response = self.client.post(
                    "/voice/synthesize", json={"text": "hello", "language": language},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "audio/wav")
                self.assertEqual(response.data[:4], b"RIFF")
                self.assertEqual(response.data[8:12], b"WAVE")
                self.assertIn("piper_model;dur=", response.headers["Server-Timing"])
                self.assertIn("piper;dur=", response.headers["Server-Timing"])
                with wave.open(io.BytesIO(response.data), "rb") as wav_file:
                    self.assertEqual((wav_file.getframerate(), wav_file.getnchannels(),
                                      wav_file.getsampwidth()), (22_050, 1, 2))
                synthesize_mock.assert_called_with("hello", language)

    @patch("voice.tts_service.synthesize_text")
    def test_json_and_field_validation(self, synthesize_mock) -> None:
        cases = (
            ({"data": "text", "content_type": "text/plain"}, 415, "unsupported_media_type"),
            ({"data": "{", "content_type": "application/json"}, 400, "invalid_json"),
            ({"json": {"language": "ko"}}, 400, "text_missing"),
            ({"json": {"text": None, "language": "ko"}}, 400, "invalid_tts_text"),
            ({"json": {"text": "   ", "language": "ko"}}, 400, "text_empty"),
            ({"json": {"text": "hello"}}, 400, "invalid_tts_language"),
            ({"json": {"text": "hello", "language": 1}}, 400, "invalid_tts_language"),
            ({"json": {"text": "hello", "language": "fr"}}, 400, "invalid_tts_language"),
        )
        for arguments, status, code in cases:
            with self.subTest(code=code, arguments=arguments):
                response = self.client.post("/voice/synthesize", **arguments)
                self.assertEqual((response.status_code, response.json["error"]["code"]),
                                 (status, code))
        synthesize_mock.assert_not_called()

    @patch("voice.tts_service.synthesize_text", return_value=synthesis())
    def test_text_length_boundary_uses_python_characters(self, synthesize_mock) -> None:
        accepted = self.client.post("/voice/synthesize", json={
            "text": "한" * MAX_TTS_TEXT_LENGTH, "language": "ko",
        })
        rejected = self.client.post("/voice/synthesize", json={
            "text": "한" * (MAX_TTS_TEXT_LENGTH + 1), "language": "ko",
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual((rejected.status_code, rejected.json["error"]["code"]),
                         (413, "text_too_long"))
        synthesize_mock.assert_called_once()

    @patch("voice.tts_service.synthesize_text")
    def test_general_one_mib_request_limit_is_preserved(self, synthesize_mock) -> None:
        response = self.client.post(
            "/voice/synthesize",
            data=b'"' + b"x" * (1024 * 1024) + b'"',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json["error"]["code"], "http_error")
        synthesize_mock.assert_not_called()

    def test_concurrent_api_request_returns_busy(self) -> None:
        entered = Event()
        release = Event()
        voice = MagicMock()

        def delayed_synthesis(_text, wav_file, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release synthesis")
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22_050)
            wav_file.writeframes(b"\1\0" * 100)

        voice.synthesize_wav.side_effect = delayed_synthesis

        def post():
            with self.app.test_client() as client:
                return client.post(
                    "/voice/synthesize", json={"text": "hello", "language": "en"},
                )

        with patch.object(tts_service, "_load_voice", return_value=voice), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(post).result(timeout=2)
                self.assertEqual((second.status_code, second.json["error"]["code"]),
                                 (429, "tts_busy"))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)

    def test_service_errors_are_sanitized_and_mapped(self) -> None:
        cases = (
            (tts_service.TtsBusyError(), 429, "tts_busy"),
            (tts_service.TtsModelMissingError(), 503, "tts_model_missing"),
            (tts_service.TtsInitializationError(), 503, "tts_initialization_failed"),
            (tts_service.TtsInferenceError(), 500, "tts_inference_failed"),
        )
        for error, status, code in cases:
            with self.subTest(code=code), patch(
                    "voice.tts_service.synthesize_text", side_effect=error):
                response = self.client.post(
                    "/voice/synthesize", json={"text": "private", "language": "en"},
                )
                self.assertEqual((response.status_code, response.json["error"]["code"]),
                                 (status, code))
                body = response.get_data(as_text=True)
                self.assertNotIn("Traceback", body)
                self.assertNotIn("F:\\", body)


class TtsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        tts_service._voices.clear()

    def tearDown(self) -> None:
        tts_service._voices.clear()

    def test_model_is_lazy_loaded_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory)
            voice_dir = model_root / "ko_KR-kss-medium"
            voice_dir.mkdir()
            (voice_dir / "ko_KR-kss-medium.onnx").touch()
            (voice_dir / "ko_KR-kss-medium.onnx.json").touch()
            fake_voice = object()
            with patch.object(tts_service, "MODEL_ROOT", model_root), \
                    patch("piper.PiperVoice.load", return_value=fake_voice) as loader:
                self.assertIs(tts_service._load_voice("ko"), fake_voice)
                self.assertIs(tts_service._load_voice("ko"), fake_voice)
            loader.assert_called_once_with(
                voice_dir / "ko_KR-kss-medium.onnx",
                config_path=voice_dir / "ko_KR-kss-medium.onnx.json",
                use_cuda=False,
            )

    def test_missing_model_and_initialization_failure_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tts_service, "MODEL_ROOT", Path(directory)):
            with self.assertRaises(tts_service.TtsModelMissingError):
                tts_service._load_voice("en")

            voice_dir = Path(directory) / "en_US-lessac-medium"
            voice_dir.mkdir()
            (voice_dir / "en_US-lessac-medium.onnx").touch()
            (voice_dir / "en_US-lessac-medium.onnx.json").touch()
            with patch("piper.PiperVoice.load", side_effect=RuntimeError("private")):
                with self.assertRaises(tts_service.TtsInitializationError):
                    tts_service._load_voice("en")

    def test_valid_memory_wav_and_no_temporary_file(self) -> None:
        voice = MagicMock()

        def write_wav(_text, wav_file, **_kwargs):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22_050)
            wav_file.writeframes(b"\1\0" * 2205)

        voice.synthesize_wav.side_effect = write_wav
        with patch.object(tts_service, "_load_voice", return_value=voice), \
                patch("tempfile.NamedTemporaryFile") as temporary_file:
            result = tts_service.synthesize_text("private", "ko")
        self.assertEqual(result.audio[:4], b"RIFF")
        self.assertEqual((result.sample_rate, result.channels, result.sample_width),
                         (22_050, 1, 2))
        self.assertAlmostEqual(result.audio_seconds, 0.35, places=3)
        self.assertAlmostEqual(result.leading_silence_seconds, 0.25, places=3)
        with wave.open(io.BytesIO(result.audio), "rb") as wav_file:
            silence_frames = round(22_050 * tts_service.LEADING_SILENCE_SECONDS)
            self.assertEqual(wav_file.readframes(silence_frames), b"\0\0" * silence_frames)
            self.assertEqual(wav_file.readframes(1), b"\1\0")
        synthesis_config = voice.synthesize_wav.call_args.kwargs["syn_config"]
        self.assertEqual(synthesis_config.length_scale, tts_service.KOREAN_LENGTH_SCALE)
        temporary_file.assert_not_called()

    def test_inference_failure_releases_lock_and_rejects_invalid_wav(self) -> None:
        voice = MagicMock()
        voice.synthesize_wav.side_effect = RuntimeError("private")
        with patch.object(tts_service, "_load_voice", return_value=voice):
            with self.assertRaises(tts_service.TtsInferenceError):
                tts_service.synthesize_text("first", "en")
            self.assertFalse(tts_service._inference_lock.locked())

            voice.synthesize_wav.side_effect = None
            voice.synthesize_wav.return_value = None
            with self.assertRaises(tts_service.TtsInferenceError):
                tts_service.synthesize_text("second", "en")
            self.assertFalse(tts_service._inference_lock.locked())

    def test_english_keeps_default_length_scale(self) -> None:
        voice = MagicMock()

        def write_wav(_text, wav_file, **_kwargs):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22_050)
            wav_file.writeframes(b"\1\0" * 100)

        voice.synthesize_wav.side_effect = write_wav
        with patch.object(tts_service, "_load_voice", return_value=voice):
            result = tts_service.synthesize_text("hello", "en")
        self.assertEqual(result.length_scale, 1.0)
        self.assertEqual(voice.synthesize_wav.call_args.kwargs["syn_config"].length_scale, 1.0)

    def test_concurrent_request_returns_busy_without_waiting(self) -> None:
        entered = Event()
        release = Event()
        voice = MagicMock()

        def delayed_synthesis(_text, wav_file, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release synthesis")
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22_050)
            wav_file.writeframes(b"\1\0" * 100)

        voice.synthesize_wav.side_effect = delayed_synthesis
        with patch.object(tts_service, "_load_voice", return_value=voice), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(tts_service.synthesize_text, "first", "en")
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(tts_service.synthesize_text, "second", "en")
                with self.assertRaises(tts_service.TtsBusyError):
                    second.result(timeout=2)
            finally:
                release.set()
            self.assertGreater(len(first.result(timeout=5).audio), 44)
