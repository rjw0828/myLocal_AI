"""C4D preparation: memory SDK only, no subprocess MCP or model inference."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from agent_poc.c4d_smoke import run_smoke, descendants, FIXTURE


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration=2
        self.temp=TemporaryDirectory(prefix='local-ai-c4d-test-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)/'experiment'

    async def test_three_modes_use_management_existing_reviewed_owner_path(self):
        prompts=[]
        result=await run_smoke(self.base,sys.executable,sys.executable,sys.executable,
            ask=lambda r:prompts.append(r) or True,output=lambda _:None)
        self.assertFalse(result['live']);self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['sdk_attempts'],3);self.assertEqual(len(prompts),1)
        self.assertEqual([c['decision']['action'] for c in result['calls']],['ASK','AUTO_APPROVE','AUTO_APPROVE'])
        self.assertEqual([c['approval_method'] for c in result['calls']],['MANUAL','AUTO','AUTO'])
        self.assertTrue(all(c['proposal_state']=='CONSUMED' for c in result['calls']))
        self.assertEqual(result['remaining_owned_pids'],[])
        self.assertEqual((self.base/'workspace'/'test.txt').read_text(encoding='utf-8'),FIXTURE)
        self.assertEqual((self.base/'workspace'/'test.txt').read_bytes(),FIXTURE.encode())

    async def test_deny_ask_sends_zero_and_does_not_continue_auto_cases(self):
        with self.assertRaises(RuntimeError):
            await run_smoke(self.base,sys.executable,sys.executable,sys.executable,
                ask=lambda _:False,output=lambda _:None)
        result=json.loads((self.base/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'],'FAIL');self.assertEqual(result['sdk_attempts'],0)
        self.assertEqual(len(result['calls']),1)
        self.assertEqual(result['calls'][0]['run']['error'],'approval_denied')
        self.assertEqual(result['remaining_owned_pids'],[])

    async def test_live_missing_start_approval_does_not_create_anything(self):
        with self.assertRaises(PermissionError):
            await run_smoke(self.base,sys.executable,sys.executable,sys.executable,
                live=True,approved_auto_scope=True,ask=lambda _:True)
        self.assertFalse(self.base.exists())

    async def test_live_missing_auto_scope_does_not_create_anything(self):
        with self.assertRaises(PermissionError):
            await run_smoke(self.base,sys.executable,sys.executable,sys.executable,
                live=True,approved_start=True,ask=lambda _:True)
        self.assertFalse(self.base.exists())

    async def test_process_tracking_excludes_unrelated_processes(self):
        self.assertEqual(descendants({11:10,12:11,50:1,10:1},10),{11,12})

    async def test_outside_root_blocked_in_every_mode_fake_only(self):
        from agent_poc.approval_cli import fixture_console
        with fixture_console(Path(self.temp.name)/'policy.sqlite3') as (policy,catalog,core,runtime,ids,root,file):
            entry=next(e for e in catalog.discover() if e.original_name=='read_file')
            policy.grant_trust(entry,resource=root,verified_read_only=True)
            for mode in ('ASK_EVERY_TIME','AUTO_ALLOW_SAFE','ALLOW_ALL'):
                policy.set_mode(mode,enable_auto=True)
                self.assertEqual(policy.evaluate(entry,{'path':str(root.parent/'outside.txt')}).action,'BLOCK')
            self.assertEqual(runtime.sends,0)


if __name__=='__main__':unittest.main()
