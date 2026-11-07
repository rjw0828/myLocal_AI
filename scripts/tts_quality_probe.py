"""Generate reproducible Korean Piper speed samples without network access."""
from __future__ import annotations

import io
import json
import struct
import sys
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from piper import PiperVoice, SynthesisConfig  # noqa: E402

from voice.tts_service import _prepend_silence  # noqa: E402

MODEL_DIR = Path(r"F:\LLM\Models\speech\piper\ko_KR-kss-medium")
OUTPUT_DIR = PROJECT_ROOT / "tests" / "tts_output"
TEXT = "안녕하세요. 로컬 AI가 음성으로 답변합니다."
SCALES = (("default", None), ("1_10", 1.10), ("1_15", 1.15), ("1_20", 1.20))


def inspect_wav(audio: bytes) -> dict[str, float | int]:
    with wave.open(io.BytesIO(audio), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    peak = max(abs(value) for value in samples)
    threshold = max(500, round(peak * 0.02))
    first_nonzero = next(index for index, value in enumerate(samples) if value != 0)
    first_audible = next(
        index for index, value in enumerate(samples) if abs(value) >= threshold
    )
    return {
        "duration_seconds": frame_count / sample_rate,
        "first_nonzero_ms": first_nonzero / sample_rate * 1000,
        "first_audible_ms": first_audible / sample_rate * 1000,
        "peak": peak,
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    loaded_at = time.perf_counter()
    voice = PiperVoice.load(
        MODEL_DIR / "ko_KR-kss-medium.onnx",
        config_path=MODEL_DIR / "ko_KR-kss-medium.onnx.json",
        use_cuda=False,
    )
    print(json.dumps({"model_load_seconds": time.perf_counter() - loaded_at}))

    for label, length_scale in SCALES:
        output = io.BytesIO()
        started = time.perf_counter()
        with wave.open(output, "wb") as wav_file:
            voice.synthesize_wav(
                TEXT,
                wav_file,
                syn_config=SynthesisConfig(length_scale=length_scale),
            )
        synthesis_seconds = time.perf_counter() - started
        audio = output.getvalue()
        path = OUTPUT_DIR / f"ko_length_{label}.wav"
        path.write_bytes(audio)
        metrics = inspect_wav(audio)
        metrics.update({
            "length_scale": length_scale or 1.0,
            "synthesis_seconds": synthesis_seconds,
            "wav_bytes": len(audio),
            "file": path.name,
        })
        print(json.dumps(metrics, ensure_ascii=False))

        if length_scale == 1.15:
            padded, silence_seconds = _prepend_silence(audio)
            padded_path = OUTPUT_DIR / "ko_length_1_15_with_250ms_silence.wav"
            padded_path.write_bytes(padded)
            with wave.open(io.BytesIO(audio), "rb") as original_wav:
                original_frames = original_wav.readframes(original_wav.getnframes())
            with wave.open(io.BytesIO(padded), "rb") as padded_wav:
                silence_frame_count = round(
                    padded_wav.getframerate() * silence_seconds
                )
                leading_frames = padded_wav.readframes(silence_frame_count)
                preserved_frames = padded_wav.readframes(padded_wav.getnframes())
            print(json.dumps({
                "file": padded_path.name,
                "leading_silence_seconds": silence_seconds,
                "leading_frames_are_zero": not any(leading_frames),
                "speech_frames_preserved": preserved_frames == original_frames,
                **inspect_wav(padded),
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
