import subprocess
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import ollama_runtime


class OllamaRuntimeTests(unittest.TestCase):
    @patch("ollama_runtime.subprocess.Popen")
    @patch("ollama_runtime.shutil.which", return_value=r"F:\LLM\Ollama\ollama.exe")
    @patch("ollama_runtime.is_online", side_effect=[False, True])
    @patch("ollama_runtime.resolve_model_store", return_value=Path(r"C:\Users\Rohn\.ollama\models"))
    def test_start_uses_fixed_serve_command(self, model_store, online, which, popen) -> None:
        self.assertEqual(ollama_runtime.start(), "started")
        self.assertEqual(popen.call_args.args[0], [r"F:\LLM\Ollama\ollama.exe", "serve"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs["env"]["OLLAMA_MODELS"],
                         r"C:\Users\Rohn\.ollama\models")

    @patch("ollama_runtime._contains_models")
    @patch("ollama_runtime.Path.home", return_value=Path(r"C:\Users\Rohn"))
    def test_model_store_falls_back_to_populated_default(self, home, contains) -> None:
        contains.side_effect = [False, True]
        with patch.dict("ollama_runtime.os.environ", {"OLLAMA_MODELS": r"F:\LLM\Models"}, clear=False):
            result = ollama_runtime.resolve_model_store()
        self.assertEqual(result, Path(r"C:\Users\Rohn\.ollama\models"))

    @patch("ollama_runtime.os.name", "nt")
    @patch("ollama_runtime.subprocess.run")
    @patch("ollama_runtime.is_online", side_effect=[True, False])
    def test_stop_uses_fixed_windows_process_target(self, online, run) -> None:
        run.return_value = MagicMock(returncode=0)
        self.assertEqual(ollama_runtime.stop(), "stopped")
        self.assertEqual(run.call_args.args[0], ["taskkill", "/IM", "ollama.exe", "/F"])
        self.assertFalse(run.call_args.kwargs["check"])

    @patch("ollama_runtime.is_online", return_value=False)
    def test_stop_is_idempotent_when_offline(self, online) -> None:
        self.assertEqual(ollama_runtime.stop(), "already_offline")


if __name__ == "__main__":
    unittest.main()
