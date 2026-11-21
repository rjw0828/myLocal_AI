"""C-3B-3: restricted test scope, isolated paths and disabled stdio preparation."""
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
import stat

from mcp_manager.models import ServerConfig
from mcp_manager.service import MCPService


class ReadinessDenied(PermissionError):
    pass


def local_path(value):
    if not isinstance(value, (str, Path)):
        raise ReadinessDenied('An explicit local path is required')
    path = Path(value)
    text = str(path)
    if (not path.is_absolute() or text.startswith(('\\\\', '//')) or '..' in path.parts
            or '\x00' in text or ':' in text[len(path.drive):]):
        raise ReadinessDenied('Relative, remote, traversal and alternate-stream paths are denied')
    return path


def no_reparse(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ReadinessDenied('Symlink and reparse-point paths are denied')


@dataclass(frozen=True)
class ReadOnlyScope:
    server_id: str
    kind: str
    root: Path | None = None
    allowed_tools: frozenset | None = None
    allow_relative: bool = False
    time_default: str | None = None

    def __post_init__(self):
        if not isinstance(self.server_id, str) or not self.server_id or self.kind not in {'time', 'filesystem'}:
            raise ReadinessDenied('Unknown server scope')
        if self.kind == 'filesystem':
            root = local_path(self.root)
            no_reparse(root)
            if not root.is_dir(): raise ReadinessDenied('Root must be an existing directory')
            object.__setattr__(self, 'root', root.resolve(strict=True))
        elif self.root is not None:
            raise ReadinessDenied('Time scope cannot contain a filesystem Root')
        if type(self.allow_relative) is not bool or (self.allow_relative and self.kind != 'filesystem'):
            raise ReadinessDenied('Relative paths require a filesystem scope')
        if self.time_default is not None and (self.kind != 'time' or self.time_default != 'Asia/Seoul'):
            raise ReadinessDenied('Only the reviewed Seoul server default is supported')
        if self.allowed_tools is not None:
            baseline = {'get_current_time'} if self.kind == 'time' else {'list_directory', 'read_file'}
            if type(self.allowed_tools) is not frozenset or not self.allowed_tools or not self.allowed_tools <= baseline:
                raise ReadinessDenied('Tool scope may only narrow the read-only baseline')

    @property
    def tools(self):
        if self.allowed_tools is not None: return self.allowed_tools
        return frozenset({'get_current_time'} if self.kind == 'time' else {'list_directory', 'read_file'})

    def validate(self, name, arguments, input_schema=None):
        if name not in self.tools or not isinstance(arguments, dict):
            raise ReadinessDenied('Tool is outside the approved read-only scope')
        if self.kind == 'time':
            if arguments == {} and self.time_default == 'Asia/Seoul':
                schema = input_schema if isinstance(input_schema, dict) else {}
                prop = schema.get('properties', {}).get('timezone', {})
                if (schema.get('type') == 'object' and isinstance(prop, dict)
                        and prop.get('type') == 'string' and prop.get('default') == self.time_default
                        and 'timezone' not in schema.get('required', [])):
                    return
                raise ReadinessDenied('Current Schema does not confirm the reviewed optional timezone default')
            if arguments != {'timezone': 'Asia/Seoul'}:
                raise ReadinessDenied('Only the exact Asia/Seoul time query is permitted')
            return
        if set(arguments) != {'path'}:
            raise ReadinessDenied('Only one explicit path argument is supported')
        value = arguments['path']
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ReadinessDenied('An explicit nonempty path string is required')
        windows = PureWindowsPath(value)
        # The server expands ~ and Windows normalizes devices, ADS and trailing
        # dots/spaces. Reject ambiguous syntax before interpreting a relative path.
        if (value.startswith(('~', '\\\\', '//', '\\??\\')) or any(ord(c) < 32 for c in value)
                or any(c in value for c in '*?"<>|') or windows.is_reserved()
                or ':' in value[len(windows.drive):]
                or (windows.drive and not windows.is_absolute())
                or (windows.root and not windows.drive)
                or any(p == '..' or (p not in ('', '.') and p.endswith(('.', ' ')))
                       or PureWindowsPath(p).is_reserved()
                       for p in value.replace('/', '\\').split('\\'))):
            raise ReadinessDenied('Ambiguous, special or traversing Windows paths are denied')
        if Path(value).is_absolute():
            candidate = local_path(value)
        elif self.allow_relative and not windows.drive and not windows.root:
            candidate = local_path(self.root / value)
        else:
            raise ReadinessDenied('Relative path interpretation has not been verified')
        no_reparse(self.root)
        no_reparse(candidate)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ReadinessDenied('Path is outside the approved Root')
        if name == 'read_file' and not resolved.is_file(): raise ReadinessDenied('Expected a file')
        if name == 'list_directory' and not resolved.is_dir(): raise ReadinessDenied('Expected a directory')


def aligned_read_only_scope(scope, config):
    """Trusted harness configuration, not model/Schema-granted permission.

    Exactly one CLI Root is required; preserve raw MCP arguments/fingerprints.
    Time default is also checked against the freshly discovered Schema per call.
    """
    if type(scope) is not ReadOnlyScope or type(config) is not ServerConfig:
        raise ReadinessDenied('Explicit reviewed scope and launch configuration required')
    if scope.kind == 'filesystem':
        if len(config.args) != 4 or config.args[1:] != ('--read-only', '--confine-roots', str(scope.root)):
            raise ReadinessDenied('Relative paths require one matching confined read-only CLI Root')
        return replace(scope, allow_relative=True)
    if config.args != ('-m', 'mcp_server_time', '--local-timezone=Asia/Seoul'):
        raise ReadinessDenied('Server launch does not confirm the Seoul default')
    return replace(scope, time_default='Asia/Seoul')


@dataclass(frozen=True)
class IsolationLayout:
    base: Path

    def __post_init__(self):
        base = local_path(self.base)
        no_reparse(base)
        base = base.resolve(strict=True)
        protected = (Path('F:/LLM/AI_Project/myLocalAI'), Path('F:/LLM/AI_Project/myLocalAI-agent-exp'))
        if any(base == root or base.is_relative_to(root) for root in protected):
            raise ReadinessDenied('Use an external isolated directory, not a project data folder')
        object.__setattr__(self, 'base', base)

    @property
    def root(self): return self.base / 'workspace'

    def service(self, runtime, profiles):
        from .owner_runtime import OwnerFakeRuntime
        if type(runtime) is not OwnerFakeRuntime or type(runtime.manager._client_factory) is not PreparedMemoryFactory:
            raise ReadinessDenied('Only the prepared memory Runtime is permitted')
        no_reparse(self.base)
        for stem in ('sessions.sqlite3', 'mcp.sqlite3'):
            for suffix in ('', '-wal', '-shm', '-journal'):
                candidate = self.base / (stem + suffix)
                if candidate.exists() or candidate.is_symlink(): no_reparse(candidate)
        if not profiles or any(type(profile) is not ServerConfig for profile in profiles):
            raise ReadinessDenied('Explicit isolated profiles are required; no environment fallback')
        # Explicit arguments bypass environment DB/profile defaults. No operating
        # DB/profile file is read or copied. Caller owns temporary-directory lifetime.
        return MCPService(session_database_path=self.base/'sessions.sqlite3',
            database_path=self.base/'mcp.sqlite3', approved_configs=tuple(profiles),
            runtime_factory=lambda: runtime)


class PreparedStdioFactory:
    """Launch manifest only. There is no live-enable flag/environment override."""
    def parameters(self, config):
        if type(config) is not ServerConfig: raise ReadinessDenied('Explicit local stdio config required')
        from mcp.client.stdio import StdioServerParameters
        return StdioServerParameters(command=config.executable, args=list(config.args), cwd=config.cwd, env=dict(config.env))

    def __call__(self, config, errlog):
        raise ReadinessDenied('Actual stdio launch requires a separate reviewed authorization step')

    def candidate(self, config, scope, verifier, errlog):
        """Build lazy official SDK objects only; the returned adapter cannot enter.

        stdio_client is an async context manager: creating it does not run its
        body or spawn a process. C-4 must explicitly review/enable entry later.
        """
        if type(scope) is not ReadOnlyScope or config.server_id != scope.server_id or not callable(verifier):
            raise ReadinessDenied('Matching server scope and owner admission verifier required')
        from mcp import Client
        from mcp.client.stdio import stdio_client
        client = Client(stdio_client(self.parameters(config), errlog=errlog))
        return PreparedOwnerAdapter(client, scope, admission_verifier=verifier)


class PreparedOwnerAdapter:
    """SDK owner lifecycle adapter. Only memory Client admission is enabled now."""
    def __init__(self, client, scope, *, admission_verifier=None):
        self.client, self.scope, self.verifier = client, scope, admission_verifier

    async def __aenter__(self):
        from .owner_runtime import OwnerFakeClient
        if type(self.client) is not OwnerFakeClient:
            raise ReadinessDenied('Real SDK Client entry remains disabled')
        await self.client.__aenter__()
        self.session = self.client.session
        return self

    async def __aexit__(self, *args): return await self.client.__aexit__(*args)

    async def list_tools(self, **kwargs):
        result = await self.client.list_tools(**kwargs)
        return result.model_copy(update={'tools': [t for t in result.tools if t.name in self.scope.tools]})

    async def before_dispatch(self):
        from .owner_runtime import OwnerFakeClient
        if type(self.client) is OwnerFakeClient: await self.client.before_dispatch()
    def validate_scope(self, name, arguments, input_schema=None):
        self.scope.validate(name, arguments, input_schema)
    def verify_admission(self, oid):
        if self.verifier is not None: self.verifier(oid)
        else: self.client.verify_admission(oid)


class PreparedMemoryFactory:
    def __init__(self, runtime): self.runtime, self.rules = runtime, {}
    def bind(self, scope):
        if type(scope) is not ReadOnlyScope or scope.server_id in self.rules:
            raise ReadinessDenied('Explicit new server scope is required')
        self.rules[scope.server_id] = scope
    def __call__(self, config, errlog):
        from .owner_runtime import OwnerFakeClient
        if config.server_id not in self.rules: raise ReadinessDenied('Server has no test scope')
        return PreparedOwnerAdapter(OwnerFakeClient(self.runtime, config), self.rules[config.server_id])
