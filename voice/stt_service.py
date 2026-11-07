"""Offline speech-to-text service backed by a local faster-whisper model."""
from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

MODEL_PATH = Path(os.environ.get(
    "LOCAL_AI_STT_MODEL_PATH",
    r"F:\LLM\Models\speech\whisper\faster-whisper-small",
))
SAMPLE_RATE = 16_000
MAX_AUDIO_SECONDS = 30.0
ALLOWED_LANGUAGES = {"auto": None, "ko": "ko", "en": "en"}
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

_inference_lock = Lock()
_model: Any = None


class SttError(RuntimeError):
    """Base class for sanitized STT failures."""


class SttBusyError(SttError):
    """Another transcription currently owns the single inference slot."""


class SttModelMissingError(SttError):
    """The configured local model is incomplete or unavailable."""


class SttInitializationError(SttError):
    """The local model could not be initialized."""


class SttAudioError(SttError):
    """The submitted bytes are not a supported, decodable audio file."""


class SttAudioTooLongError(SttError):
    """The decoded audio exceeds the configured duration limit."""


class SttInferenceError(SttError):
    """The model failed while transcribing valid decoded audio."""


class SttEmptyResultError(SttError):
    """The model completed without producing usable text."""


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str
    audio_seconds: float


def _load_model() -> Any:
    global _model
    if _model is not None:
        return _model
    if (not MODEL_PATH.is_dir()
            or any(not (MODEL_PATH / name).is_file() for name in REQUIRED_MODEL_FILES)):
        raise SttModelMissingError()
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(MODEL_PATH),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
    except SttModelMissingError:
        raise
    except Exception as error:
        raise SttInitializationError() from error
    _model = model
    return model


def _decode_audio(audio_bytes: bytes) -> tuple[Any, float]:
    if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        return _decode_pcm_wav(audio_bytes)
    try:
        import av
        from faster_whisper.audio import decode_audio

        probe = av.open(io.BytesIO(audio_bytes))
        try:
            format_names = {
                name.strip()
                for name in (probe.format.name or "").lower().split(",")
                if name.strip()
            }
            supported = bool(format_names & {
                "wav", "wave", "mov", "mp4", "m4a", "3gp", "3g2", "mj2",
                "matroska", "webm",
            })
            if not supported or not probe.streams.audio:
                raise SttAudioError()
        finally:
            probe.close()
        decoded = decode_audio(io.BytesIO(audio_bytes), sampling_rate=SAMPLE_RATE)
    except SttAudioError:
        raise
    except Exception as error:
        raise SttAudioError() from error
    if getattr(decoded, "size", 0) == 0:
        raise SttAudioError()
    audio_seconds = len(decoded) / SAMPLE_RATE
    if audio_seconds > MAX_AUDIO_SECONDS:
        raise SttAudioTooLongError()
    return decoded, audio_seconds


def _decode_pcm_wav(audio_bytes: bytes) -> tuple[Any, float]:
    """Decode the browser-generated mono PCM WAV without FFmpeg demuxing."""
    try:
        import numpy as np

        with wave.open(io.BytesIO(audio_bytes), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            frames = source.readframes(frame_count)
        if (channels != 1 or sample_width != 2 or sample_rate <= 0 or compression != "NONE"):
            raise SttAudioError()
        audio_seconds = frame_count / sample_rate
        if audio_seconds > MAX_AUDIO_SECONDS:
            raise SttAudioTooLongError()
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0:
            raise SttAudioError()
        if sample_rate != SAMPLE_RATE:
            output_size = round(samples.size * SAMPLE_RATE / sample_rate)
            if output_size <= 0:
                raise SttAudioError()
            source_positions = np.arange(samples.size, dtype=np.float64) / sample_rate
            target_positions = np.arange(output_size, dtype=np.float64) / SAMPLE_RATE
            samples = np.interp(target_positions, source_positions, samples).astype(np.float32)
        return samples, audio_seconds
    except (SttAudioError, SttAudioTooLongError):
        raise
    except Exception as error:
        raise SttAudioError() from error


def transcribe_audio(audio_bytes: bytes, language: str = "auto") -> Transcription:
    """Decode and transcribe one upload without waiting for another request."""
    if language not in ALLOWED_LANGUAGES:
        raise ValueError("Unsupported language")
    if not _inference_lock.acquire(blocking=False):
        raise SttBusyError()
    try:
        decoded, audio_seconds = _decode_audio(audio_bytes)
        model = _load_model()
        try:
            segments, info = model.transcribe(
                decoded,
                beam_size=1,
                language=ALLOWED_LANGUAGES[language],
                vad_filter=False,
            )
            text = "".join(segment.text for segment in segments).strip()
        except Exception as error:
            raise SttInferenceError() from error
        if not text:
            raise SttEmptyResultError()
        detected = language if language != "auto" else getattr(info, "language", "")
        if detected not in {"ko", "en"}:
            detected = str(detected or "unknown")
        return Transcription(text=text, language=detected, audio_seconds=audio_seconds)
    finally:
        _inference_lock.release()
