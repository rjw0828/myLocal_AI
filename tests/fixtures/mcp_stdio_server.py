"""Real SDK fixture: only echo/add, no stdout except MCP protocol bytes.

All diagnostic artifacts are explicitly supplied TemporaryDirectory paths by
the integration test. No conversation, model, network, or application DB access.
"""
import argparse
import json
import os
from pathlib import Path
import time

import anyio
from mcp.server.mcpserver import MCPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--events-file", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--startup-delay", type=float, default=0)
    parser.add_argument("--fail-start", action="store_true")
    options = parser.parse_args()

    def event(phase, tool=None):
        with Path(options.events_file).open("a", encoding="utf-8") as output:
            output.write(json.dumps({"phase": phase, "tool": tool, "at": time.monotonic()}) + "\n")

    Path(options.pid_file).write_text(json.dumps({
        "pid": os.getpid(), "parent_pid": os.getppid(), "token": options.token,
    }), encoding="utf-8")
    event("boot")
    if options.fail_start:
        raise SystemExit(2)
    time.sleep(options.startup_delay)
    server = MCPServer("Local AI MCP-1 fixture", log_level="ERROR")

    @server.tool()
    async def echo(text: str) -> str:
        event("start", "echo")
        await anyio.sleep(options.delay)
        event("end", "echo")
        return text

    @server.tool()
    async def add(a: int, b: int) -> int:
        event("start", "add")
        await anyio.sleep(options.delay)
        event("end", "add")
        return a + b

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
