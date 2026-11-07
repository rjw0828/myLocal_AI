"""Offline text-to-speech service backed by local Piper voice models."""
from __future__ import annotations

import io
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

MODEL_ROOT = Path(os.environ.get(
    "LOCAL_AI_TTS_MODEL_ROOT",
    r"F:\LLM\Models\speech\piper",
))
VOICE_NAMES = {
    "ko": "ko_KR-kss-medium",
    "en": "en_US-lessac-medium",
}
EXPECTED_SAMPLE_RATE = 22_050
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2
KOREAN_LENGTH_SCALE = 1.15
LEADING_SILENCE_SECONDS = 0.25

_inference_lock = Lock()
_voices: dict[str, Any] = {}


class TtsError(RuntimeError):
    """Base class for sanitized TTS failures."""


class TtsBusyError(TtsError):
    """Another synthesis currently owns the single inference slot."""


class TtsModelMissingError(TtsError):
    """The configured local voice model is incomplete or unavailable."""


class TtsInitializationError(TtsError):
    """A local Piper voice could not be initialized."""


class TtsInferenceError(TtsError):
    """Piper failed to produce a valid WAV response."""


@dataclass(frozen=True)
class Synthesis:
    audio: bytes
    language: str
    sample_rate: int
    channels: int
    sample_width: int
    audio_seconds: float
    model_load_seconds: float = 0.0
    synthesis_seconds: float = 0.0
    leading_silence_seconds: float = 0.0
    length_scale: float = 1.0


def _voice_paths(language: str) -> tuple[Path, Path]:
    try:
        voice_name = VOICE_NAMES[language]
    except KeyError as error:
        raise ValueError("Unsupported language") from error
    voice_dir = MODEL_ROOT / voice_name
    return voice_dir / f"{voice_name}.onnx", voice_dir / f"{voice_name}.onnx.json"


def _load_voice(language: str) -> Any:
    voice = _voices.get(language)
    if voice is not None:
        return voice
    model_path, config_path = _voice_paths(language)
    if not model_path.is_file() or not config_path.is_file():
        raise TtsModelMissingError()
    try:
        from piper import PiperVoice

        voice = PiperVoice.load(
            model_path,
            config_path=config_path,
            use_cuda=False,
        )
    except Exception as error:
        raise TtsInitializationError() from error
    _voices[language] = voice
    return voice


def _validate_wav(audio: bytes, language: str) -> Synthesis:
    try:
        if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
            raise TtsInferenceError()
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()
        if (channels != EXPECTED_CHANNELS
                or sample_width != EXPECTED_SAMPLE_WIDTH
                or sample_rate != EXPECTED_SAMPLE_RATE
                or compression != "NONE" or frame_count <= 0):
            raise TtsInferenceError()
        return Synthesis(
            audio=audio,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            audio_seconds=frame_count / sample_rate,
        )
    except TtsInferenceError:
        raise
    except Exception as error:
        raise TtsInferenceError() from error


def _prepend_silence(audio: bytes, seconds: float = LEADING_SILENCE_SECONDS) -> tuple[bytes, float]:
    """Add PCM silence without altering Piper's generated speech frames."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            compression = source.getcomptype()
            frames = source.readframes(source.getnframes())
        if (channels != EXPECTED_CHANNELS
                or sample_width != EXPECTED_SAMPLE_WIDTH
                or sample_rate != EXPECTED_SAMPLE_RATE
                or compression != "NONE"):
            raise TtsInferenceError()
        silence_frames = round(sample_rate * seconds)
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(sample_width)
            target.setframerate(sample_rate)
            target.writeframes(b"\0" * silence_frames * channels * sample_width)
            target.writeframes(frames)
        return output.getvalue(), silence_frames / sample_rate
    except TtsInferenceError:
        raise
    except Exception as error:
        raise TtsInferenceError() from error


def synthesize_text(text: str, language: str) -> Synthesis:
    """Synthesize one text without waiting behind another TTS request."""
    if language not in VOICE_NAMES:
        raise ValueError("Unsupported language")
    if not _inference_lock.acquire(blocking=False):
        raise TtsBusyError()
    try:
        model_started = time.perf_counter()
        voice = _load_voice(language)
        model_load_seconds = time.perf_counter() - model_started
        length_scale = KOREAN_LENGTH_SCALE if language == "ko" else 1.0
        output = io.BytesIO()
        try:
            from piper import SynthesisConfig

            synthesis_started = time.perf_counter()
            with wave.open(output, "wb") as wav_file:
                voice.synthesize_wav(
                    text,
                    wav_file,
                    syn_config=SynthesisConfig(length_scale=length_scale),
                )
            synthesis_seconds = time.perf_counter() - synthesis_started
            raw_audio = output.getvalue()
            _validate_wav(raw_audio, language)
            audio, silence_seconds = _prepend_silence(raw_audio)
        except Exception as error:
            raise TtsInferenceError() from error
        validated = _validate_wav(audio, language)
        return Synthesis(
            audio=validated.audio,
            language=validated.language,
            sample_rate=validated.sample_rate,
            channels=validated.channels,
            sample_width=validated.sample_width,
            audio_seconds=validated.audio_seconds,
            model_load_seconds=model_load_seconds,
            synthesis_seconds=synthesis_seconds,
            leading_silence_seconds=silence_seconds,
            length_scale=length_scale,
        )
    finally:
        _inference_lock.release()
