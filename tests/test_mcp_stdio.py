"""Actual SDK/stdin/stdout/Windows process integration, separate from fakes.

Never discovers or kills Python processes by name. Termination uses a retained
Windows handle to the token-verified fixture child only, with its image checked.
"""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

from mcp_manager import MCPManager, MCPRuntimeError, MCPTimeoutError, OperationState, ServerConfig, ServerState


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("fixture condition did not become true")


class ProcessProbe:
    def __init__(self, pid, *, terminate=False, expected_images=()):
        self.pid, self.handle = pid, None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                         ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        self.kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.kernel.WaitForSingleObject.restype = ctypes.c_ulong
        self.kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.kernel.TerminateProcess.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        if pid == os.getpid() or pid <= 0:
            raise AssertionError("refusing to target test runner")
        rights = 0x00100000 | 0x1000 | (0x0001 if terminate else 0)
        self.handle = self.kernel.OpenProcess(rights, False, pid)
        if not self.handle:
            if ctypes.get_last_error() == 87:
                return  # Already exited before observation.
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        if self.exited():
            return  # A signalled handle proves exit; image lookup can now fail.
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            if self.exited():
                return
            self.close()
            raise AssertionError("cannot verify fixture executable image")
        if os.path.normcase(os.path.realpath(buffer.value)) not in {
                os.path.normcase(os.path.realpath(path)) for path in expected_images}:
            self.close()
            raise AssertionError("fixture process image does not match expected Python")
        self.can_terminate = terminate

    def exited(self):
        return self.handle is None or self.kernel.WaitForSingleObject(self.handle, 0) == 0

    def terminate(self):
        if not self.exited():
            if not self.can_terminate or not self.kernel.TerminateProcess(self.handle, 73):
                raise AssertionError("could not terminate the exact fixture child")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@unittest.skipUnless(os.name == "nt", "Windows stdio process integration requires Windows")
class MCPStdioTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="local-ai-mcp1-")
        self.managers, self.probes, self.artifacts = [], [], {}
        self.addCleanup(self.cleanup_runtime)

    def cleanup_runtime(self):
        errors = []
        for manager in reversed(self.managers):
            try:
                manager.shutdown()
            except Exception as error:
                errors.append(error)
        for probe in self.probes:
            try:
                wait_for(probe.exited)
            except AssertionError as error:
                errors.append(error)
                # Emergency cleanup is restricted to our retained child handle;
                # the test still FAILS if normal runtime cleanup leaked it.
                if getattr(probe, "can_terminate", False):
                    probe.terminate()
            finally:
                probe.close()
        self.directory.cleanup()
        self.assertEqual(errors, [], "MCP process cleanup was not confirmed")

    def config(self, name="one", *extra):
        token = uuid4().hex
        directory = Path(self.directory.name)
        pid_file = directory / f"{name}.pid.json"
        events = directory / f"{name}.events.jsonl"
        self.artifacts[name] = pid_file, events, token
        return ServerConfig(name, sys.executable, (
            str(FIXTURE.resolve()), "--pid-file", str(pid_file), "--events-file", str(events),
            "--token", token, *extra,
        ), str(directory))

    def manager(self, *configs, **kwargs):
        manager = MCPManager(configs or [self.config()], **kwargs)
        self.managers.append(manager)
        return manager

    def observe(self, name="one"):
        path, _events, token = self.artifacts[name]
        wait_for(path.exists)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["token"], token)
        probe = ProcessProbe(record["pid"], terminate=True,
                             expected_images=[sys.executable, sys._base_executable])
        self.probes.append(probe)
        # Windows venv redirectors can add a launcher parent. Observe it, but
        # never grant its handle termination rights or target the test runner.
        parent = record["parent_pid"]
        if parent != os.getpid():
            wrapper = ProcessProbe(parent, expected_images=[sys.executable])
            self.probes.append(wrapper)
        return probe

    def events(self, name="one"):
        path = self.artifacts[name][1]
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_real_echo_add_and_normal_process_exit(self):
        manager = self.manager()
        self.assertEqual(manager.start_server("one").state, ServerState.RUNNING)
        probe = self.observe()
        self.assertEqual([tool.name for tool in manager.list_tools("one")], ["echo", "add"])
        echo = manager.call_tool("one", "echo", {"text": "안녕하세요 MCP"})
        added = manager.call_tool("one", "add", {"a": 19, "b": 23})
        self.assertFalse(echo.is_error)
        self.assertEqual(echo.content[0]["text"], "안녕하세요 MCP")
        self.assertEqual(added.content[0]["text"], "42")
        self.assertEqual(manager.stop_server("one").state, ServerState.STOPPED)
        wait_for(probe.exited)

    def test_concurrent_duplicate_start_spawns_only_one_server(self):
        manager = self.manager()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(manager.start_server, ["one", "one"]))
        self.observe()
        self.assertTrue(all(status.state == ServerState.RUNNING for status in results))
        manager.start_server("one")
        self.assertEqual(len([event for event in self.events() if event["phase"] == "boot"]), 1)

    def test_two_real_servers_are_independent(self):
        manager = self.manager(self.config("one", "--delay", "0.7"), self.config("two"))
        manager.start_server("one")
        self.observe("one")
        manager.start_server("two")
        self.observe("two")
        with ThreadPoolExecutor(1) as pool:
            slow = pool.submit(manager.call_tool, "one", "echo", {"text": "slow"})
            wait_for(lambda: any(event["phase"] == "start" for event in self.events("one")))
            result = manager.call_tool("two", "add", {"a": 2, "b": 3})
            self.assertEqual(result.content[0]["text"], "5")
            self.assertFalse(slow.done())
            slow.result(5)

    def test_real_calls_are_serialized(self):
        manager = self.manager(self.config("one", "--delay", "0.1"))
        manager.start_server("one")
        self.observe()
        with ThreadPoolExecutor(2) as pool:
            calls = [pool.submit(manager.call_tool, "one", "echo", {"text": str(i)}) for i in range(2)]
            for call in calls:
                self.assertFalse(call.result(5).is_error)
        self.assertEqual([event["phase"] for event in self.events()], ["boot", "start", "end", "start", "end"])

    def test_failed_server_start_exits_and_can_be_stopped(self):
        manager = self.manager(self.config("one", "--fail-start"))
        with self.assertRaises(MCPRuntimeError) as error:
            manager.start_server("one")
        self.assertEqual(error.exception.code, "mcp_start_failed")
        self.observe()
        self.assertEqual(manager.get_status("one").state, ServerState.ERROR)
        self.assertEqual(manager.stop_server("one").state, ServerState.STOPPED)

    def test_real_timeout_blocks_retry_and_late_result_is_retained(self):
        manager = self.manager(self.config("one", "--delay", "0.25"))
        manager.start_server("one")
        self.observe()
        with self.assertRaises(MCPTimeoutError) as timeout:
            manager.call_tool("one", "echo", {"text": "late"}, timeout=0.02)
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        with self.assertRaises(MCPRuntimeError):
            manager.call_tool("one", "echo", {"text": "no duplicate"})
        wait_for(lambda: manager.get_status("one").state == ServerState.RUNNING)
        outcome = manager.get_operation("one", timeout.exception.operation_id)
        self.assertEqual(outcome.state, OperationState.SUCCEEDED)
        self.assertEqual(outcome.result.content[0]["text"], "late")
        self.assertEqual(len([event for event in self.events() if event["phase"] == "start"]), 1)

    def test_forced_exit_and_explicit_reconnect(self):
        manager = self.manager()
        manager.start_server("one")
        old = self.observe()
        old.terminate()
        wait_for(old.exited)
        with self.assertRaises(MCPRuntimeError) as error:
            manager.call_tool("one", "echo", {"text": "after crash"}, timeout=3)
        self.assertEqual(error.exception.code, "mcp_call_uncertain")
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        manager.stop_server("one")
        manager.start_server("one")
        new = self.observe()
        self.assertNotEqual(old.pid, new.pid)
        self.assertEqual(manager.call_tool("one", "echo", {"text": "again"}).content[0]["text"], "again")

    def test_three_start_stop_cycles_have_no_remaining_children(self):
        manager = self.manager()
        for _ in range(3):
            manager.start_server("one")
            probe = self.observe()
            manager.stop_server("one")
            wait_for(probe.exited)
        self.assertEqual(len([event for event in self.events() if event["phase"] == "boot"]), 3)

    def test_shutdown_cancels_busy_sdk_owner_and_reaps_processes(self):
        manager = self.manager(self.config("one", "--delay", "10"), stop_timeout=0.05)
        manager.start_server("one")
        probe = self.observe()
        with ThreadPoolExecutor(2) as pool:
            call = pool.submit(manager.call_tool, "one", "echo", {"text": "in flight"})
            wait_for(lambda: any(event["phase"] == "start" for event in self.events()))
            shutdown = pool.submit(manager.shutdown)
            wait_for(lambda: manager.get_status("one").state == ServerState.STOPPING)
            with self.assertRaises(MCPRuntimeError):
                manager.call_tool("one", "echo", {"text": "rejected"})
            shutdown.result(15)
            with self.assertRaises(MCPRuntimeError):
                call.result(3)
        wait_for(probe.exited)
        self.assertFalse(manager._host.running)

    def test_sdk_tool_error_is_returned_without_losing_connection(self):
        manager = self.manager()
        manager.start_server("one")
        self.observe()
        invalid = manager.call_tool("one", "add", {"a": "not-an-integer", "b": 3})
        self.assertTrue(invalid.is_error)
        self.assertEqual(manager.get_status("one").state, ServerState.RUNNING)
        self.assertFalse(manager.call_tool("one", "add", {"a": 2, "b": 3}).is_error)

    def test_start_timeout_then_stop_cleans_up_before_handshake(self):
        manager = self.manager(self.config("one", "--startup-delay", "10"), stop_timeout=0.05)
        with self.assertRaises(MCPTimeoutError):
            manager.start_server("one", timeout=0.02)
        probe = self.observe()
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        manager.stop_server("one")
        wait_for(probe.exited)
        self.assertEqual(manager.get_status("one").state, ServerState.STOPPED)


if __name__ == "__main__":
    unittest.main()
