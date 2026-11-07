"""Narrow process controls for the loopback-only Ollama server."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import ai_controller

START_TIMEOUT_SECONDS = 8


class OllamaControlError(RuntimeError):
    """The requested fixed Ollama lifecycle action could not complete."""


def _contains_models(directory: Path) -> bool:
    for manifest_dir in (directory / "manifests", directory / "manifests-v2"):
        if manifest_dir.is_dir() and any(path.is_file() for path in manifest_dir.rglob("*")):
            return True
    return False


def resolve_model_store() -> Path | None:
    """Prefer a populated configured store, otherwise reuse Ollama's default store."""
    configured = os.environ.get("OLLAMA_MODELS")
    configured_path = Path(configured) if configured else None
    default_path = Path.home() / ".ollama" / "models"
    if configured_path and _contains_models(configured_path):
        return configured_path
    if _contains_models(default_path):
        return default_path
    return configured_path


def is_online() -> bool:
    try:
        ai_controller.check_ollama()
    except (OSError, TimeoutError, ValueError):
        return False
    return True


def start() -> str:
    """Start `ollama serve` without accepting arbitrary command input."""
    if is_online():
        return "already_online"
    executable = shutil.which("ollama")
    if not executable:
        raise OllamaControlError("Ollama 실행 파일을 찾을 수 없습니다.")
    environment = os.environ.copy()
    model_store = resolve_model_store()
    if model_store:
        environment["OLLAMA_MODELS"] = str(model_store)
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen([executable, "serve"], **kwargs)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_online():
            return "started"
        time.sleep(0.2)
    raise OllamaControlError("Ollama 서버가 제한 시간 안에 시작되지 않았습니다.")


def stop() -> str:
    """Stop only Ollama processes; no shell or caller-supplied arguments."""
    if not is_online():
        return "already_offline"
    if os.name != "nt":
        raise OllamaControlError("현재 운영체제에서는 웹 종료를 지원하지 않습니다.")
    result = subprocess.run(
        ["taskkill", "/IM", "ollama.exe", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise OllamaControlError("Ollama 프로세스를 종료하지 못했습니다.")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not is_online():
            return "stopped"
        time.sleep(0.2)
    raise OllamaControlError("Ollama 서버가 종료 상태로 전환되지 않았습니다.")
