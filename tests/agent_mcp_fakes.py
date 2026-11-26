"""Generic fixtures only: no SDK transport, OS reads or approval files."""
from copy import deepcopy
from types import SimpleNamespace

from mcp_manager.service import MCPService
from mcp_manager.registry import fingerprint


SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
          'required': ['value'], 'additionalProperties': False}


class FakeMCPService:
    def __init__(self):
        self.servers = {}
        self._connection_errors = {}
        self.executions = 0
        self.validations = 0
        self.approval = None
        self.result = {'value': 42}

    def add(self, sid='alpha', name='convert', **changes):
        server = self.servers.setdefault(sid, {'state': 'RUNNING', 'approved': True, 'tools': []})
        tool = {'name': name, 'description': 'Generic fixture operation', 'input_schema': deepcopy(SCHEMA),
                'output_schema': {'type': 'object'}, 'annotations': {'readOnlyHint': True},
                'allowed': True, 'source': {'config_hash': 'fixture-config',
                'connection_generation': 'connection-1', 'permission_generation': 1, 'schema_version': 1}}
        tool.update(changes)
        server['tools'].append(tool)
        return tool

    def list_servers(self):
        return [{'server_id': sid, 'status': {'state': server['state']},
                 'enabled': server.get('enabled',True),
                 'execution_approved': server['approved']} for sid, server in self.servers.items()]

    def catalog_tools(self, sid):
        server = self.servers[sid]
        if server['state'] != 'RUNNING' or not server['approved']:
            raise PermissionError('Server unavailable')
        return deepcopy(server['tools'])

    def validate_catalog_arguments(self, sid, name, arguments, expected_fingerprint):
        self.validations += 1
        tool = next((t for t in self.catalog_tools(sid) if t['name'] == name), None)
        if tool is None or not tool['allowed'] or fingerprint(tool) != expected_fingerprint:
            raise PermissionError('Stale mapping')
        return MCPService._arguments(SimpleNamespace(input_schema=tool['input_schema']), arguments)

    def call_tool(self, *args, **kwargs):
        self.executions += 1
        raise AssertionError('C-2 must never execute even a fake service tool')

    def approve_call(self, *args, **kwargs):
        raise AssertionError('C-2 must not issue approval tokens')
