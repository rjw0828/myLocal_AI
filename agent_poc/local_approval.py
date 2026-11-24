"""Local console decisions only; all authority stays in the server ApprovalCore."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys

from .approval_core import ApprovalError


def discard_pending_console_input():
    """Discard pre-prompt paste/Enter events; they cannot express fresh consent."""
    if sys.platform != 'win32' or not sys.stdin.isatty():
        raise ApprovalError('local_interactive_console_required')
    import msvcrt
    for _ in range(512):
        if not msvcrt.kbhit(): return
        msvcrt.getwch()
    raise ApprovalError('local_console_input_overflow')


async def console_line(seconds):
    """Bounded Windows console stdin, without an abandoned blocking input thread.

    Redirected input is deliberately rejected: this harness requires a human
    console. Tests inject an asynchronous reader instead of a live keyboard.
    """
    if sys.platform != 'win32' or not sys.stdin.isatty():
        return None
    import msvcrt
    deadline = asyncio.get_running_loop().time() + seconds
    chars = []
    while asyncio.get_running_loop().time() < deadline:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ('\x03', '\x04', '\x1a'): return None
            if char in ('\x00', '\xe0'):
                msvcrt.getwch()
                continue
            if char in ('\r', '\n'):
                print(flush=True)
                return ''.join(chars)
            if char == '\b':
                if chars:
                    chars.pop(); print('\b \b', end='', flush=True)
            elif char.isprintable() and len(chars) < 200:
                chars.append(char); print(char, end='', flush=True)
        await asyncio.sleep(.02)
    return None


async def request_decision(turn, proposal_id, *, reader=console_line, output=print):
    """Show a fresh snapshot, accept one exact decision, and revalidate before use.

    Does not resume an Agent, issue a Proposal, mint a token or execute a tool.
    No input, invalid input and cancellation terminate this Run fail-closed.
    """
    core, rid = turn.core, turn.run_id
    snapshot = core.proposal(rid, proposal_id)
    status = core.status(rid)
    if snapshot['state'] != 'PENDING' or status['state'] != 'WAITING_APPROVAL':
        raise ApprovalError('local_proposal_not_pending')
    remaining = min(snapshot['expires_at'], status['deadline'] - core.call_seconds) - core.clock()
    if remaining <= 0: raise ApprovalError('local_execution_budget_insufficient')
    if reader is console_line:
        # Only input entered after the forthcoming prompt may authorize this call.
        discard_pending_console_input()
    output(json.dumps({'run_id': rid, 'proposal_id': proposal_id,
        'server_id': snapshot['server_id'], 'tool': snapshot['tool_name'],
        'arguments': json.loads(snapshot['arguments_json']),
        'expires_utc_estimate': (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat(),
        'decision_seconds_remaining': remaining,
        'notice': f'One exact call only. Run timeout includes approval waiting; {core.call_seconds}s execution budget reserved.'},
        ensure_ascii=False, indent=2))
    output(f'For this displayed request only: type y to Allow or n to Deny, then Enter. Proposal: {proposal_id}')
    try:
        line = await asyncio.wait_for(reader(remaining), timeout=remaining)
        latest = core.proposal(rid, proposal_id)
        if latest != snapshot or core.status(rid)['state'] != 'WAITING_APPROVAL':
            raise ApprovalError('local_proposal_changed')
        if core.clock() >= status['deadline'] - core.call_seconds:
            raise ApprovalError('local_execution_budget_insufficient')
        if line not in ('y', 'n', f'ALLOW {proposal_id}', f'DENY {proposal_id}'):
            raise ApprovalError('local_no_valid_decision')
        allow = line == 'y' or line.startswith('ALLOW ')
        turn.decide(proposal_id, allow)  # Server revalidates fingerprints and expiry.
        return allow
    except BaseException:
        core.cancel(rid)
        raise
