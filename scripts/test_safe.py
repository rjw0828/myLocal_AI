"""Reviewed Python suite, excluding SDK/stdio and Voice dependency modules.

Excluded modules are not imported, skipped or counted as passing. Other tests
may launch temporary Python test stubs/Fake CLIs; they do not authorize MCP I/O.
"""
from pathlib import Path
import os
import sys
import unittest

EXCLUDED = {'test_mcp_api_stdio.py', 'test_mcp_stdio.py', 'test_stt_api.py', 'test_tts_api.py'}


def main():
    os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / 'tests')]
    suite = unittest.TestSuite()
    for file in sorted((root / 'tests').glob('test_*.py')):
        if file.name not in EXCLUDED:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(file.stem))
    count = suite.countTestCases()
    print('SAFE_TEST_COUNT', count, 'EXCLUDED', sorted(EXCLUDED), flush=True)
    if not count:
        return 1
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
