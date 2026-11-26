"""Immutable, model-independent MCP catalog. No execution or approval API."""
from dataclasses import dataclass
import json
import re

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from mcp_manager.registry import canonical_json, fingerprint


class CatalogError(RuntimeError):
    pass


def validate_schema(schema):
    """Offline JSON Schema 2020-12 subset: object root, local refs only.

    Unknown format assertions and required vocabularies are rejected rather
    than silently ignored. No schema coercion, reference downloads or defaults.
    """
    try:
        encoded = canonical_json(schema)
        if len(encoded.encode('utf-8')) > 65536:
            raise ValueError('schema size limit')
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ValueError('schema root must be an object')
        if schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema') != 'https://json-schema.org/draft/2020-12/schema':
            raise ValueError('unsupported schema dialect')
        checker = FormatChecker()

        from referencing.jsonschema import DRAFT202012
        resolver = Registry().resolver_with_root(DRAFT202012.create_resource(schema))
        maps = {'properties', 'patternProperties', '$defs', 'dependentSchemas'}
        singles = {'items', 'additionalProperties', 'unevaluatedProperties', 'propertyNames',
                   'contains', 'unevaluatedItems', 'not', 'if', 'then', 'else', 'contentSchema'}
        arrays = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}

        def visit(node):
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key in ('$ref', '$dynamicRef'):
                    if not isinstance(value, str) or not value.startswith('#'):
                        raise ValueError('external schema references are unsupported')
                    resolver.lookup(value)
                if key == '$vocabulary':
                    raise ValueError('custom schema vocabularies are unsupported')
                if key == 'format' and value not in checker.checkers:
                    raise ValueError('unsupported schema format')
                if key == '$schema' and value != 'https://json-schema.org/draft/2020-12/schema':
                    raise ValueError('unsupported schema dialect')
                # Embedded resources complicate local reference scope; reject
                # explicitly in this PoC rather than resolve against the wrong root.
                if key == '$id':
                    raise ValueError('embedded schema resources are unsupported')
                if key in maps:
                    for child in value.values(): visit(child)
                elif key in singles:
                    visit(value)
                elif key in arrays:
                    for child in value: visit(child)
        Draft202012Validator.check_schema(schema)
        visit(schema)
    except Exception as exc:
        raise CatalogError('Unsupported or invalid JSON Schema') from exc


def safe_alias(server_id, name):
    stem = re.sub('[^a-zA-Z0-9_]', '_', name)[:36] or 'tool'
    return 'mcp_' + stem + '_' + fingerprint([server_id, name])[:16]


@dataclass(frozen=True)
class CatalogEntry:
    server_id: str
    original_name: str
    alias: str
    descriptor_json: str
    identity: str

    @property
    def descriptor(self):
        return json.loads(self.descriptor_json)


class MCPCatalog:
    """Snapshots pin source metadata; refresh never silently upgrades old tools."""
    def __init__(self, service, *, max_tools=512, max_bytes=4 * 1024 * 1024, isolate_failures=False):
        self.service = service
        self.max_tools, self.max_bytes = max_tools, max_bytes
        self.isolate_failures=isolate_failures

    def discover(self):
        entries, aliases, pairs, total_bytes = [], set(), set(), 0
        for server in self.service.list_servers():
            if not server['enabled'] or not server['execution_approved'] or server['status']['state'] != 'RUNNING':
                continue
            sid = server['server_id']
            from mcp_manager.errors import MCPServiceError
            from mcp_manager.models import MCPRuntimeError
            try:
                tools=self.service.catalog_tools(sid)
                names=set()
                for tool in tools:
                    name=tool['name']
                    if not isinstance(name,str) or not name or name in names:raise CatalogError('Invalid source tool')
                    names.add(name);validate_schema(tool['input_schema'])
                    if tool.get('output_schema') is not None:validate_schema(tool['output_schema'])
                    if not all(k in tool.get('source',{}) for k in ('config_hash','connection_generation','permission_generation')):
                        raise CatalogError('Missing source identity')
            except (MCPServiceError,MCPRuntimeError,CatalogError,KeyError,TypeError):
                if not self.isolate_failures:raise
                self.service._connection_errors[sid]='mcp_catalog_unavailable'
                continue  # A failed server cannot hide healthy peers.
            errors=getattr(self.service,'_connection_errors',{})
            if errors.get(sid)=='mcp_catalog_unavailable':errors.pop(sid,None)
            for tool in tools:
                if not tool['allowed']:
                    continue
                name = tool['name']
                if not isinstance(name, str) or not name or (sid, name) in pairs:
                    raise CatalogError('Duplicate or invalid source tool')
                pairs.add((sid, name))
                validate_schema(tool['input_schema'])
                if tool.get('output_schema') is not None:
                    validate_schema(tool['output_schema'])
                source = tool.get('source', {})
                if not all(key in source for key in ('config_hash', 'connection_generation', 'permission_generation')):
                    raise CatalogError('Missing trusted source identity')
                alias = safe_alias(sid, name)
                if alias in aliases:
                    raise CatalogError('Model tool name collision')
                aliases.add(alias)
                encoded = canonical_json(tool)
                total_bytes += len(encoded.encode('utf-8'))
                if len(entries) >= self.max_tools or total_bytes > self.max_bytes:
                    raise CatalogError('Complete catalog exceeds configured resource limit')
                entries.append(CatalogEntry(sid, name, alias, encoded, fingerprint(tool)))
        return tuple(entries)

    def validate(self, entry, arguments):
        # Public service boundary refreshes permission, schema and transport
        # identity each time. It never creates a token or executes a tool.
        arguments = self.service.validate_catalog_arguments(
            entry.server_id, entry.original_name, arguments, entry.identity)
        Draft202012Validator(entry.descriptor['input_schema'], registry=Registry(),
                             format_checker=FormatChecker()).validate(arguments)
        return arguments
