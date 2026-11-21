"""Virtual fixtures: no time lookup, file access, subprocess or MCP connection."""
from pathlib import PureWindowsPath

ROOT = r'F:\LLM\MCP_Workspace'
FILE = ROOT + r'\test.txt'
STAMP = '2026-11-17T09:30:00+09:00'
CONTENT = 'Local AI Lab test, value=314159'

def approved_call(name, args):
    if name == 'get_current_time':
        return set(args) <= {'timezone'} and args.get('timezone', 'Asia/Seoul') == 'Asia/Seoul'
    if name in ('list_directory', 'read_file'):
        expected = ROOT if name == 'list_directory' else FILE
        return set(args) == {'path'} and isinstance(args['path'], str) and PureWindowsPath(args['path']) == PureWindowsPath(expected)
    if name == 'convert_temperature':
        return set(args) == {'celsius'} and type(args['celsius']) in (int, float) and args['celsius'] in (0, 25, 100)
    return False

def toolset(ledger, *, extension=False):
    from pydantic_ai import FunctionToolset
    tools = FunctionToolset(requires_approval=True, sequential=True, max_retries=0, timeout=2)
    def record(name, args):
        if not approved_call(name, args):
            raise ValueError('Outside virtual fixture domain')
        ledger.append({'tool': name, 'arguments': args})

    @tools.tool_plain
    def get_current_time(timezone: str = 'Asia/Seoul') -> dict:
        """Query the current date/time in an IANA timezone. Virtual evaluation data."""
        record('get_current_time', {'timezone': timezone})
        return {'datetime': STAMP, 'timezone': 'Asia/Seoul', 'day_of_week': 'Tuesday'}

    @tools.tool_plain
    def list_directory(path: str) -> dict:
        """List files in the directory at an absolute Windows path. Does not read file contents."""
        record('list_directory', {'path': path})
        return {'path': ROOT, 'files': ['test.txt']}

    @tools.tool_plain
    def read_file(path: str) -> dict:
        """Read text from a file at an absolute Windows path. Use list_directory to discover files when needed."""
        record('read_file', {'path': path})
        return {'path': FILE, 'content': CONTENT}

    if extension:
        @tools.tool_plain
        def convert_temperature(celsius: float) -> dict:
            """Convert a Celsius temperature to Fahrenheit."""
            record('convert_temperature', {'celsius': celsius})
            return {'celsius': celsius, 'fahrenheit': celsius * 9 / 5 + 32}
    return tools
