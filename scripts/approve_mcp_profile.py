"""Interactive local-owner approval; never executes a proposed program."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_manager.profile_requests import DEFAULT_APPROVALS, ProfileRequests, approve_request
from mcp_manager.registry import DEFAULT_MCP_DB_PATH


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_id")
    parser.add_argument("--requests-dir", type=Path,
                        default=Path(os.environ.get("LOCAL_AI_MCP_DB_PATH", DEFAULT_MCP_DB_PATH)).resolve().parent / "mcp_profile_requests")
    parser.add_argument("--approvals-file", type=Path, default=Path(os.environ.get("LOCAL_AI_MCP_APPROVALS", DEFAULT_APPROVALS)))
    options = parser.parse_args()

    def confirm(record):
        print("LOCAL OWNER REVIEW: this program will run with your account's filesystem/network permissions.")
        print("No command is executed by this approval script.")
        for key in ("name", "transport", "executable", "args", "cwd"):
            print(key + " (JSON): " + json.dumps(record[key], ensure_ascii=True))
        print("Approval file: " + json.dumps(str(options.approvals_file.resolve()), ensure_ascii=True))
        return input("Type APPROVE " + record["request_id"] + " to persist this exact configuration: ").strip() == "APPROVE " + record["request_id"]

    try:
        accepted = approve_request(ProfileRequests(options.requests_dir, options.approvals_file), options.request_id, confirm)
    except (OSError, ValueError, RuntimeError) as error:
        print("Approval failed: " + str(error), file=sys.stderr)
        return 1
    print("Approved. Return to Add Server and check approval to register." if accepted else "Not approved. No settings changed.")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
