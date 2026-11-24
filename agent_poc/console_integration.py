"""Opt-in wiring for the existing Console; no separate Registry or Agent loop."""
import hashlib
import logging
import os
from pathlib import Path

import ai_controller
from .approval_policy import ApprovalPolicy
from .mcp_management import MCPManagement
from .web_backend import AgentWebBackend
from .web_contract import ToolChatError
from .web_console import local_model


def policy_path(session_path):
    # Keep policy separate from operating DBs/profiles. Existing path guards stay.
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')
    identity = hashlib.sha256(str(Path(session_path).resolve()).encode()).hexdigest()[:24]
    return local/'LocalAILab'/'agent-console'/identity/'approval_policy.sqlite3'


def backend_factory(selection, policy_file):
    def factory(service, revision, prepare, commit):
        logger = logging.getLogger('local_ai.agent')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
            logger.addHandler(handler)
        manager = MCPManagement.attach(service, ApprovalPolicy(policy_file))
        def resolve():
            name = selection.current()
            try:
                if name not in ai_controller.list_models():
                    raise ValueError('not installed')
            except (OSError, ValueError):
                raise ToolChatError('agent_model_unavailable') from None
            return name
        return AgentWebBackend(manager, session_revision=revision, prepare=prepare, commit=commit,
            model_name=selection.current(), model_resolver=resolve,
            model_factory=lambda initial: local_model(initial, name=initial['agent_model'], installed_only=True))
    return factory
