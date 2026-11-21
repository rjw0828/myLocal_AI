"""Process-local outbound firewall. Install before third-party imports.

This protects Python socket connections, not the already running Ollama daemon
or arbitrary native code. A violation fails the acceptance check even if caught.
"""
import os
import sys
import socket

VIOLATIONS = []
INSTALLED = False

def install():
    global INSTALLED
    if INSTALLED:
        return
    os.environ['OTEL_SDK_DISABLED'] = 'true'
    os.environ['OTEL_TRACES_EXPORTER'] = 'none'
    os.environ['OTEL_METRICS_EXPORTER'] = 'none'
    os.environ['OTEL_LOGS_EXPORTER'] = 'none'
    os.environ['LOGFIRE_SEND_TO_LOGFIRE'] = 'false'
    os.environ['DO_NOT_TRACK'] = '1'
    os.environ['PYDANTIC_AI_NO_BANNER'] = '1'
    for name in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','GOOGLE_API_KEY','GEMINI_API_KEY','LOGFIRE_TOKEN'):
        os.environ.pop(name,None)
    def audit(event, args):
        if event == 'socket.connect':
            address = args[1]
            # Windows asyncio creates its own local self-pipe using the stdlib
            # socketpair fallback. Permit only that exact caller, not arbitrary
            # localhost services.
            if (isinstance(address, tuple) and address[0] == '127.0.0.1'
                    and sys._getframe(1).f_code is socket.socketpair.__code__):
                return
            if not isinstance(address, tuple) or address[:2] != ('127.0.0.1', 11434):
                VIOLATIONS.append({'event': event, 'address': str(address)})
                raise PermissionError('PoC forbids non-Ollama outbound connections')
        elif event == 'socket.getaddrinfo' and args[0] not in ('127.0.0.1', b'127.0.0.1'):
            VIOLATIONS.append({'event': event, 'host': str(args[0])})
            raise PermissionError('PoC forbids external DNS')
    sys.addaudithook(audit)
    INSTALLED = True
