"""Core lifecycle/concurrency tests with fake clients; no real processes/models."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from threading import Event, Lock
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest

import anyio

from mcp_manager import MCPManager, MCPRuntimeError, MCPTimeoutError, OperationState, ServerConfig, ServerState


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


class FakeText:
    def __init__(self, text):
        self.text = text

    def model_dump(self, **_kwargs):
        return {"type": "text", "text": self.text}


class Scenario:
    def __init__(self):
        self.start_gate, self.call_gate = Event(), Event()
        self.start_gate.set()
        self.call_gate.set()
        self.entered, self.call_started, self.exited = Event(), Event(), Event()
        self.fail_start, self.fail_call = False, False
        self.starts, self.live, self.active, self.max_active = 0, 0, 0, 0
        self.calls = []
        self.guard = Lock()
        self.owner = None
        self.same_owner_on_exit = False

    async def wait(self, gate):
        while not gate.is_set():
            await anyio.sleep(0.002)

    @asynccontextmanager
    async def client(self, _config, errlog):
        self.assert_stderr_is_not_console = Path(errlog.name).name.lower() in {"nul", "null"}
        self.starts += 1
        self.live += 1
        self.owner = asyncio.current_task()
        self.entered.set()
        self.exited.clear()
        try:
            await self.wait(self.start_gate)
            if self.fail_start:
                raise RuntimeError("SECRET internal path should never escape")
            yield self
        finally:
            self.same_owner_on_exit = self.owner is asyncio.current_task()
            self.live -= 1
            self.exited.set()

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="echo", description=None,
                                                     input_schema={"type": "object"})])

    async def call_tool(self, name, arguments):
        with self.guard:
            self.active += 1
            self.max_active = max(self.active, self.max_active)
            self.calls.append((name, arguments))
        self.call_started.set()
        try:
            await self.wait(self.call_gate)
            if self.fail_call:
                raise RuntimeError("SECRET transport error")
            return SimpleNamespace(content=[FakeText(arguments.get("text", "result"))],
                                   structured_content=None, is_error=name == "invalid")
        finally:
            with self.guard:
                self.active -= 1


class MCPRuntimeTests(unittest.TestCase):
    def config(self, server_id="one"):
        return ServerConfig(server_id, sys.executable)

    def manager(self, scenarios=None, **kwargs):
        scenarios = scenarios or {"one": Scenario()}
        manager = MCPManager([self.config(key) for key in scenarios],
                             _client_factory=lambda config, err: scenarios[config.server_id].client(config, err),
                             **kwargs)
        self.addCleanup(manager.shutdown)
        for scenario in scenarios.values():
            self.addCleanup(scenario.start_gate.set)
            self.addCleanup(scenario.call_gate.set)
        return manager, scenarios

    def test_host_is_shared_and_last_manager_stops_it(self):
        a, _ = self.manager()
        b, _ = self.manager()
        self.assertIs(a._host, b._host)
        self.assertTrue(a._host.running)
        a.shutdown()
        self.assertTrue(b._host.running)
        b.shutdown()
        self.assertFalse(b._host.running)

    def test_server_id_has_only_one_owner_across_managers(self):
        first, _ = self.manager()
        second, scenarios = self.manager()
        first.start_server("one")
        with self.assertRaises(MCPRuntimeError) as error:
            second.start_server("one")
        self.assertEqual(error.exception.code, "mcp_server_owned")
        self.assertEqual(scenarios["one"].starts, 0)
        first.stop_server("one")
        self.assertEqual(second.start_server("one").state, ServerState.RUNNING)

    def test_construction_does_not_start_servers_and_defaults_are_correct(self):
        manager, scenarios = self.manager()
        self.assertEqual(manager.start_timeout, 15)
        self.assertEqual(manager.call_timeout, 30)
        self.assertEqual(manager.get_status("one").state, ServerState.STOPPED)
        self.assertEqual(scenarios["one"].starts, 0)

    def test_unregistered_server_never_starts_a_client(self):
        manager, scenarios = self.manager()
        with self.assertRaises(MCPRuntimeError) as error:
            manager.start_server("unknown")
        self.assertEqual(error.exception.code, "mcp_not_registered")
        self.assertEqual(scenarios["one"].starts, 0)

    def test_start_stop_and_same_task_owns_context(self):
        manager, scenarios = self.manager()
        self.assertEqual(manager.start_server("one").state, ServerState.RUNNING)
        self.assertEqual(manager.list_tools("one")[0].name, "echo")
        self.assertEqual(manager.call_tool("one", "echo", {"text": "hello"}).content[0]["text"], "hello")
        self.assertEqual(manager.stop_server("one").state, ServerState.STOPPED)
        scenario = scenarios["one"]
        self.assertEqual(scenario.live, 0)
        self.assertTrue(scenario.same_owner_on_exit)
        self.assertTrue(scenario.assert_stderr_is_not_console)

    def test_simultaneous_duplicate_start_uses_one_context(self):
        manager, scenarios = self.manager()
        scenario = scenarios["one"]
        scenario.start_gate.clear()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(manager.start_server, "one")
            self.assertTrue(scenario.entered.wait(2))
            second = pool.submit(manager.start_server, "one")
            scenario.start_gate.set()
            self.assertEqual(first.result(3).state, ServerState.RUNNING)
            self.assertEqual(second.result(3).state, ServerState.RUNNING)
        manager.start_server("one")
        self.assertEqual(scenario.starts, 1)

    def test_duplicate_stop_is_safe(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(manager.stop_server, ["one", "one"]))
        self.assertTrue(all(status.state == ServerState.STOPPED for status in results))
        self.assertEqual(scenarios["one"].live, 0)

    def test_different_servers_do_not_block_each_other(self):
        manager, scenarios = self.manager({"one": Scenario(), "two": Scenario()})
        manager.start_server("one")
        manager.start_server("two")
        scenarios["one"].call_gate.clear()
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(manager.call_tool, "one", "echo", {"text": "one"})
            self.assertTrue(scenarios["one"].call_started.wait(2))
            result = manager.call_tool("two", "echo", {"text": "two"})
            self.assertEqual(result.content[0]["text"], "two")
            self.assertFalse(first.done())
            scenarios["one"].call_gate.set()
            first.result(3)

    def test_same_server_calls_are_serialized(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.call_gate.clear()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(manager.call_tool, "one", "echo", {"text": "first"})
            self.assertTrue(scenario.call_started.wait(2))
            second = pool.submit(manager.call_tool, "one", "echo", {"text": "second"})
            wait_for(lambda: manager.get_status("one").pending_operations == 1)
            self.assertEqual(len(scenario.calls), 1)
            scenario.call_gate.set()
            self.assertEqual(first.result(3).content[0]["text"], "first")
            self.assertEqual(second.result(3).content[0]["text"], "second")
        self.assertEqual(scenario.max_active, 1)

    def test_timeout_blocks_new_call_and_retains_late_result(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.call_gate.clear()
        with self.assertRaises(MCPTimeoutError) as timeout:
            manager.call_tool("one", "echo", {"text": "late"}, timeout=0.05)
        op_id = timeout.exception.operation_id
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        self.assertEqual(manager.get_operation("one", op_id).state, OperationState.RUNNING)
        with self.assertRaises(MCPRuntimeError):
            manager.call_tool("one", "echo", {"text": "must not run"})
        with self.assertRaises(MCPRuntimeError):
            manager.start_server("one")
        scenario.call_gate.set()
        wait_for(lambda: manager.get_status("one").state == ServerState.RUNNING)
        outcome = manager.get_operation("one", op_id)
        self.assertEqual(outcome.state, OperationState.SUCCEEDED)
        self.assertEqual(outcome.result.content[0]["text"], "late")
        self.assertEqual(len(scenario.calls), 1)

    def test_start_timeout_tracks_late_initialization(self):
        manager, scenarios = self.manager()
        scenarios["one"].start_gate.clear()
        with self.assertRaises(MCPTimeoutError) as timeout:
            manager.start_server("one", timeout=0.05)
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        scenarios["one"].start_gate.set()
        wait_for(lambda: manager.get_status("one").state == ServerState.RUNNING)
        self.assertEqual(manager.get_operation("one", timeout.exception.operation_id).state, OperationState.SUCCEEDED)

    def test_initialization_failure_cleans_up_and_can_retry(self):
        manager, scenarios = self.manager()
        scenario = scenarios["one"]
        scenario.fail_start = True
        with self.assertRaises(MCPRuntimeError) as error:
            manager.start_server("one")
        self.assertNotIn("SECRET", str(error.exception))
        wait_for(lambda: scenario.live == 0)
        self.assertEqual(manager.get_status("one").state, ServerState.ERROR)
        scenario.fail_start = False
        self.assertEqual(manager.start_server("one").state, ServerState.RUNNING)

    def test_transport_error_cleans_up_and_requires_restart(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.fail_call = True
        with self.assertRaises(MCPRuntimeError) as error:
            manager.call_tool("one", "echo")
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(error.exception.code, "mcp_call_uncertain")
        wait_for(lambda: scenario.live == 0)
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        self.assertEqual(manager.get_operation("one", error.exception.operation_id).state, OperationState.UNCERTAIN)
        with self.assertRaises(MCPRuntimeError):
            manager.start_server("one")
        scenario.fail_call = False
        manager.stop_server("one")  # Await the failed owner's context cleanup.
        manager.start_server("one")
        self.assertEqual(manager.call_tool("one", "echo", {"text": "again"}).content[0]["text"], "again")

    def test_timeout_followed_by_lost_response_stays_uncertain_until_stop(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.fail_call = True
        scenario.call_gate.clear()
        with self.assertRaises(MCPTimeoutError) as timeout:
            manager.call_tool("one", "echo", timeout=0.05)
        scenario.call_gate.set()
        wait_for(lambda: manager.get_operation("one", timeout.exception.operation_id).state == OperationState.UNCERTAIN)
        self.assertEqual(manager.get_status("one").state, ServerState.UNCERTAIN)
        with self.assertRaises(MCPRuntimeError):
            manager.start_server("one")
        self.assertEqual(manager.stop_server("one").state, ServerState.STOPPED)

    def test_tool_is_error_does_not_break_connection(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        result = manager.call_tool("one", "invalid", {})
        self.assertTrue(result.is_error)
        self.assertEqual(manager.get_status("one").state, ServerState.RUNNING)
        self.assertFalse(manager.call_tool("one", "echo").is_error)
        self.assertEqual(scenarios["one"].starts, 1)

    def test_stop_cancels_hung_start_and_closes_partial_resources(self):
        manager, scenarios = self.manager(stop_timeout=0.05)
        scenario = scenarios["one"]
        scenario.start_gate.clear()
        with self.assertRaises(MCPTimeoutError):
            manager.start_server("one", timeout=0.05)
        self.assertEqual(manager.stop_server("one").state, ServerState.STOPPED)
        self.assertEqual(scenario.live, 0)
        self.assertTrue(scenario.same_owner_on_exit)

    def test_shutdown_cancels_busy_owner_and_rejects_new_requests(self):
        manager, scenarios = self.manager(stop_timeout=0.05)
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.call_gate.clear()
        with ThreadPoolExecutor(3) as pool:
            call = pool.submit(manager.call_tool, "one", "echo")
            self.assertTrue(scenario.call_started.wait(2))
            shutdown = pool.submit(manager.shutdown)
            wait_for(lambda: manager.get_status("one").state == ServerState.STOPPING)
            with self.assertRaises(MCPRuntimeError):
                manager.call_tool("one", "echo")
            duplicate = pool.submit(manager.shutdown)
            shutdown.result(3)
            duplicate.result(3)
            with self.assertRaises(MCPRuntimeError):
                call.result(3)
        self.assertEqual(scenario.live, 0)
        self.assertFalse(manager._host.running)
        self.assertEqual(manager.get_status("one").state, ServerState.STOPPED)

    def test_stop_drains_completed_call_before_context_exit(self):
        manager, scenarios = self.manager(stop_timeout=2)
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.call_gate.clear()
        with ThreadPoolExecutor(2) as pool:
            call = pool.submit(manager.call_tool, "one", "echo", {"text": "drained"})
            self.assertTrue(scenario.call_started.wait(2))
            stop = pool.submit(manager.stop_server, "one")
            wait_for(lambda: manager.get_status("one").state == ServerState.STOPPING)
            scenario.call_gate.set()
            self.assertEqual(call.result(3).content[0]["text"], "drained")
            self.assertEqual(stop.result(3).state, ServerState.STOPPED)

    def test_queue_timeout_does_not_execute_abandoned_request(self):
        manager, scenarios = self.manager()
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.call_gate.clear()
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(manager.call_tool, "one", "echo", {"text": "active"})
            self.assertTrue(scenario.call_started.wait(2))
            with self.assertRaises(MCPTimeoutError) as timeout:
                manager.call_tool("one", "echo", {"text": "abandoned"}, timeout=0.05)
            scenario.call_gate.set()
            first.result(3)
            wait_for(lambda: manager.get_operation("one", timeout.exception.operation_id).state == OperationState.CANCELLED)
        self.assertEqual(len(scenario.calls), 1)

    def test_invalid_config_and_inputs(self):
        with self.assertRaises(ValueError):
            ServerConfig("one", "python")
        with self.assertRaises(ValueError):
            ServerConfig("one", sys.executable, args="not argv")
        with TemporaryDirectory(prefix="local-ai-mcp-config-") as directory:
            shell = Path(directory) / "cmd.exe"
            shell.touch()
            with self.assertRaises(ValueError):
                ServerConfig("shell", str(shell))
        with self.assertRaises(ValueError):
            ServerConfig("one", sys.executable, args=("embedded\x00nul",))
        with self.assertRaises(ValueError):
            MCPManager([self.config(), self.config()])
        manager, _ = self.manager()
        for value in [0, -1, float("inf"), float("nan"), True]:
            with self.assertRaises(ValueError):
                manager.start_server("one", timeout=value)
        for value in [[], {"invalid": float("nan")}, {"oversized": "x" * 65536}]:
            with self.assertRaises(ValueError):
                manager.call_tool("one", "echo", value)

    def test_sync_api_on_loop_thread_fails_without_allocating_a_worker(self):
        manager, scenarios = self.manager()

        async def start_from_loop():
            with self.assertRaises(MCPRuntimeError) as error:
                manager.start_server("one")
            self.assertEqual(error.exception.code, "mcp_sync_on_loop")

        manager._host.submit(start_from_loop()).result(2)
        self.assertEqual(manager.get_status("one").state, ServerState.STOPPED)
        self.assertEqual(scenarios["one"].starts, 0)
        manager.start_server("one")

        async def shutdown_from_loop():
            with self.assertRaises(MCPRuntimeError):
                manager.shutdown()

        manager._host.submit(shutdown_from_loop()).result(2)
        self.assertEqual(manager.get_status("one").state, ServerState.RUNNING)

    def test_shutdown_rejects_queued_calls_without_executing_them(self):
        manager, scenarios = self.manager(stop_timeout=0.05)
        manager.start_server("one")
        scenario = scenarios["one"]
        scenario.call_gate.clear()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(manager.call_tool, "one", "echo", {"text": "active"})
            self.assertTrue(scenario.call_started.wait(2))
            queued = pool.submit(manager.call_tool, "one", "echo", {"text": "queued"})
            wait_for(lambda: manager.get_status("one").pending_operations == 1)
            manager.shutdown()
            for request in (first, queued):
                with self.assertRaises(MCPRuntimeError):
                    request.result(2)
        self.assertEqual(len(scenario.calls), 1)

    def test_retained_results_are_bounded_and_caller_cannot_mutate_them(self):
        manager, _ = self.manager()
        manager.start_server("one")
        result = manager.call_tool("one", "echo", {"text": "original"})
        op_id = manager.get_status("one").last_operation.operation_id
        result.content[0]["text"] = "mutated"
        self.assertEqual(manager.get_operation("one", op_id).result.content[0]["text"], "original")
        for _ in range(20):
            manager.call_tool("one", "echo")
        with self.assertRaises(MCPRuntimeError):
            manager.get_operation("one", op_id)


if __name__ == "__main__":
    unittest.main()
