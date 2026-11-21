"""Policy decisions and revocation use fixtures, memory SDK and temporary DBs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from dataclasses import replace
import subprocess
import sys
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agent_poc.approval_policy import ApprovalPolicy, ApprovalMode, PolicyError
from agent_poc.approval_cli import fixture_console, call_fixture
from agent_poc.approval_core import ApprovalError
from agent_poc.mcp_catalog import CatalogEntry
from mcp_manager.registry import canonical_json, fingerprint
from mcp_manager.models import ToolInfo


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=TemporaryDirectory(prefix='local-ai-policy-test-');self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'policy.sqlite3'
        context=fixture_console(self.path);self.addCleanup(context.__exit__,None,None,None)
        self.policy,self.catalog,self.core,self.runtime,self.ids,self.root,self.file=context.__enter__()
        self.entries={e.original_name:e for e in self.catalog.discover()}

    async def asyncTearDown(self):
        self.runtime.dispatch_gate.set();self.runtime.finish_gate.set();await self.core.drain_fixtures()

    def prepare(self,name='get_current_time',args=None):
        session='s-'+str(len(self.core._sessions));self.core.create_session(session);rid=self.core.start_run(session)
        args={} if args is None else args;entry=self.entries[name]
        p=self.core.issue(rid,'call',entry,args);return rid,p,entry,args

    def safe(self,name='get_current_time',resource=None,mode='AUTO_ALLOW_SAFE'):
        tid=self.policy.grant_trust(self.entries[name],resource=resource,verified_read_only=True)
        self.policy.set_mode(mode,enable_auto=True);return tid

    async def execute(self,values):
        rid,p,entry,args=values
        return await self.core.execute(rid,'call',entry,args)

    async def test_default_product_mode_does_not_implicitly_enable_auto(self):
        state=self.policy.snapshot()
        self.assertEqual(state['project_mode'],'AUTO_ALLOW_SAFE');self.assertFalse(state['auto_enabled'])
        self.assertEqual(self.policy.evaluate(self.entries['get_current_time'],{}).action,'ASK')

    async def test_ask_allow_once_existing_deferred_cli(self):
        self.policy.set_mode('ASK_EVERY_TIME')
        async def reader(_):return 'y'
        result=await call_fixture(self.core,self.entries['get_current_time'],{},reader=reader,output=lambda _:None)
        self.assertEqual(result['run']['state'],'COMPLETED');self.assertEqual(self.runtime.sends,1)

    async def test_ask_deny_no_send(self):
        self.policy.set_mode('ASK_EVERY_TIME')
        async def reader(_):return 'n'
        result=await call_fixture(self.core,self.entries['get_current_time'],{},reader=reader,output=lambda _:None)
        self.assertEqual(result['run']['error'],'approval_denied');self.assertEqual(self.runtime.sends,0)

    async def test_safe_time_auto_and_audit(self):
        self.safe();values=self.prepare();rid,p,entry,args=values
        self.assertEqual(self.core.apply_policy(rid,p['proposal_id']).action,'AUTO_APPROVE')
        self.assertEqual(self.runtime.sends,0)
        await self.execute(values)
        self.assertEqual(self.runtime.sends,1)
        events=self.policy.audit_log()
        self.assertEqual(sum(e['event']=='operation_admitted' for e in events),1)
        self.assertTrue(any(e['event']=='operation_outcome' and e['state']=='COMPLETED' for e in events))
        self.assertNotIn('approval_token',str(events))

    async def test_safe_relative_file_auto(self):
        self.safe('read_file',self.root);values=self.prepare('read_file',{'path':'test.txt'})
        self.assertEqual(self.core.apply_policy(values[0],values[1]['proposal_id']).action,'AUTO_APPROVE')
        await self.execute(values);self.assertEqual(self.runtime.sends,1)

    async def test_hint_alone_cannot_auto_approve(self):
        old=self.entries['read_file'];descriptor=old.descriptor
        descriptor['annotations']={'readOnlyHint':True}
        entry=CatalogEntry(old.server_id,old.original_name,old.alias,canonical_json(descriptor),fingerprint(descriptor))
        self.policy.grant_trust(entry,resource=self.root,verified_read_only=False)
        self.policy.set_mode('AUTO_ALLOW_SAFE',enable_auto=True)
        self.assertEqual(self.policy.evaluate(entry,{'path':'test.txt'}).action,'ASK')

    async def test_allow_all_trusted_scope_auto(self):
        self.policy.grant_trust(self.entries['read_file'],resource=self.file,verified_read_only=False)
        self.policy.set_mode('ALLOW_ALL',enable_auto=True)
        values=self.prepare('read_file',{'path':'test.txt'})
        self.assertEqual(self.core.apply_policy(values[0],values[1]['proposal_id']).action,'AUTO_APPROVE')
        await self.execute(values);self.assertEqual(self.runtime.sends,1)

    async def test_allow_all_untrusted_server_asks(self):
        self.safe(mode='ALLOW_ALL')
        self.assertEqual(self.policy.evaluate(self.entries['read_file'],{'path':'test.txt'}).action,'ASK')

    async def test_untrusted_tool_is_not_added_to_existing_trust(self):
        self.safe('read_file',self.root)
        self.assertEqual(self.policy.evaluate(self.entries['list_directory'],{'path':'.'}).action,'ASK')

    async def test_new_generic_tool_in_safe_mode_asks(self):
        entry=self.entries['get_current_time'];d=entry.descriptor
        d['name']='new_counter';d['source']['config_hash']='other-server'
        new=CatalogEntry('new-server','new_counter','new_alias',canonical_json(d),fingerprint(d))
        self.policy.set_mode('AUTO_ALLOW_SAFE',enable_auto=True)
        self.assertEqual(self.policy.evaluate(new,{}).action,'ASK')

    async def test_allow_all_generic_non_readonly_fake_policy(self):
        # Policy-only: no non-readonly call is sent even to the memory SDK.
        d=deepcopy(self.entries['get_current_time'].descriptor);d['name']='fake_increment'
        d['annotations']={'readOnlyHint':False};d['input_schema']={'type':'object'}
        new=CatalogEntry('fake-counter','fake_increment','counter',canonical_json(d),fingerprint(d))
        self.policy.grant_trust(new,verified_read_only=False)
        self.policy.set_mode('AUTO_ALLOW_SAFE',enable_auto=True)
        self.assertEqual(self.policy.evaluate(new,{}).action,'ASK')
        self.policy.set_mode('ALLOW_ALL',enable_auto=True)
        self.assertEqual(self.policy.evaluate(new,{}).action,'AUTO_APPROVE')
        self.assertEqual(self.runtime.sends,0)

    async def test_root_external_is_block_even_allow_all(self):
        self.safe('read_file',self.root,mode='ALLOW_ALL')
        d=self.policy.evaluate(self.entries['read_file'],{'path':str(Path(__file__).resolve())})
        self.assertEqual(d.action,'BLOCK')

    async def test_narrow_file_trust_does_not_auto_expand(self):
        other=self.root/'other.txt';other.write_text('synthetic',encoding='utf-8')
        self.safe('read_file',self.file)
        self.assertEqual(self.policy.evaluate(self.entries['read_file'],{'path':'other.txt'}).action,'ASK')

    async def test_schema_or_connection_change_requires_review(self):
        self.safe()
        for change in ('schema','connection','permission','config','root_contract'):
            with self.subTest(change=change):
                old=self.entries['get_current_time'];d=deepcopy(old.descriptor)
                if change=='schema':d['input_schema']['description']='changed'
                else:d['source'][{'connection':'connection_generation','permission':'permission_generation',
                    'config':'config_hash','root_contract':'config_hash'}[change]]='changed'
                new=CatalogEntry(old.server_id,old.original_name,old.alias,canonical_json(d),fingerprint(d))
                self.assertEqual(self.policy.evaluate(new,{}).action,'ASK')

    async def test_server_override_ask_beats_project_auto(self):
        self.safe();self.policy.set_mode('ASK_EVERY_TIME',server_id=self.ids['time'])
        self.assertEqual(self.policy.evaluate(self.entries['get_current_time'],{}).action,'ASK')

    async def test_override_removal_and_disable(self):
        self.safe();self.policy.set_mode('ASK_EVERY_TIME',server_id=self.ids['time'])
        self.policy.clear_override(self.ids['time'])
        self.assertEqual(self.policy.evaluate(self.entries['get_current_time'],{}).action,'AUTO_APPROVE')
        self.policy.disable_auto();self.assertEqual(self.policy.evaluate(self.entries['get_current_time'],{}).action,'ASK')

    async def test_revoke_after_auto_before_execution_blocks(self):
        tid=self.safe();values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id'])
        self.policy.revoke_trust(tid)
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.runtime.sends,0)

    async def test_mode_change_invalidates_manual_pending_proposal(self):
        values=self.prepare();self.policy.set_mode('ASK_EVERY_TIME')
        with self.assertRaises(ApprovalError):self.core.decide(values[0],values[1]['proposal_id'],True)
        self.assertEqual(self.runtime.sends,0)

    async def test_separate_policy_instance_revocation_is_seen(self):
        tid=self.safe();values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id'])
        second=ApprovalPolicy(self.path);second.revoke_trust(tid)
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.runtime.sends,0)

    async def test_duplicate_auto_and_operation_do_not_send_twice(self):
        self.safe();values=self.prepare();rid,p,entry,args=values
        self.core.apply_policy(rid,p['proposal_id'])
        with self.assertRaises(ApprovalError):self.core.apply_policy(rid,p['proposal_id'])
        await self.execute(values)
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.runtime.sends,1)

    async def test_expired_trust_does_not_auto(self):
        self.policy.clock=lambda:100.
        self.policy.grant_trust(self.entries['get_current_time'],verified_read_only=True,expires_at=101.)
        self.policy.set_mode('AUTO_ALLOW_SAFE',enable_auto=True);self.policy.clock=lambda:102.
        self.assertEqual(self.policy.evaluate(self.entries['get_current_time'],{}).action,'ASK')

    async def test_trust_expiry_after_auto_is_rechecked_at_dispatch(self):
        self.policy.clock=lambda:100.
        self.policy.grant_trust(self.entries['get_current_time'],verified_read_only=True,expires_at=101.)
        self.policy.set_mode('AUTO_ALLOW_SAFE',enable_auto=True)
        values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id']);self.policy.clock=lambda:102.
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.runtime.sends,0)

    async def test_cancelled_proposal_cannot_auto(self):
        self.safe();rid,p,entry,args=self.prepare();self.core.cancel(rid)
        with self.assertRaises(ApprovalError):self.core.apply_policy(rid,p['proposal_id'])
        self.assertEqual(self.runtime.sends,0)

    async def test_denied_proposal_cannot_auto(self):
        self.safe();rid,p,entry,args=self.prepare();self.core.decide(rid,p['proposal_id'],False)
        with self.assertRaises(ApprovalError):self.core.apply_policy(rid,p['proposal_id'])
        self.assertEqual(self.runtime.sends,0)

    async def test_persistence_preserves_modes_and_pinned_trust(self):
        self.safe()
        loaded=ApprovalPolicy(self.path,contracts=self.policy._contracts)
        self.assertEqual(loaded.snapshot(),self.policy.snapshot())
        self.assertEqual(loaded.evaluate(self.entries['get_current_time'],{}).action,'AUTO_APPROVE')

    async def test_revoke_at_owner_checkpoint_prevents_send(self):
        tid=self.safe();values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id'])
        self.runtime.dispatch_gate.clear();task=asyncio.create_task(self.execute(values))
        for _ in range(500):
            if self.runtime.dispatch_waiting.is_set():break
            await asyncio.sleep(.002)
        else:self.fail('Owner checkpoint absent')
        self.policy.revoke_trust(tid);self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError):await task
        self.assertEqual(self.runtime.sends,0)

    async def test_foreign_timezone_block_cannot_manual_override(self):
        self.safe();rid,p,entry,args=self.prepare(args={'timezone':'UTC'})
        self.assertEqual(self.core.apply_policy(rid,p['proposal_id']).action,'BLOCK')
        with self.assertRaises(ApprovalError):self.core.decide(rid,p['proposal_id'],True)
        self.assertEqual(self.runtime.sends,0)

    async def test_concurrent_auto_decisions_admit_only_once(self):
        self.safe();values=self.prepare();rid,p,entry,args=values
        def decide(_):
            try:return self.core.apply_policy(rid,p['proposal_id']).action
            except ApprovalError:return 'REJECTED'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(decide,range(2)))
        self.assertEqual(results.count('AUTO_APPROVE'),1)
        await self.execute(values);self.assertEqual(self.runtime.sends,1)

    async def test_changed_root_contract_is_not_implicitly_trusted(self):
        self.safe('read_file',self.root)
        other=self.root.parent/'new-root';other.mkdir();(other/'test.txt').write_text('fixture',encoding='utf-8')
        scopes=dict(self.policy._contracts)
        scopes[self.ids['filesystem']]=replace(scopes[self.ids['filesystem']],root=other)
        reconnected=ApprovalPolicy(self.path,contracts=scopes)
        self.assertEqual(reconnected.evaluate(self.entries['read_file'],{'path':'test.txt'}).action,'ASK')

    async def test_policy_edit_failure_rolls_back(self):
        before=self.policy.snapshot();original=self.policy._audit
        def fail(*args,**kwargs):raise OSError('synthetic audit failure')
        self.policy._audit=fail
        try:
            with self.assertRaises(OSError):self.policy.set_mode('ALLOW_ALL',enable_auto=True)
        finally:self.policy._audit=original
        self.assertEqual(before,self.policy.snapshot())

    async def test_automatic_audit_failure_cannot_execute(self):
        self.safe();values=self.prepare();original=self.policy.audit
        def fail(*args,**kwargs):raise OSError('synthetic audit failure')
        self.policy.audit=fail
        try:
            with self.assertRaises(OSError):self.core.apply_policy(values[0],values[1]['proposal_id'])
        finally:self.policy.audit=original
        self.assertEqual(self.runtime.sends,0)

    async def test_proposal_expiry_after_automatic_decision(self):
        self.safe();now=[100.];self.core.clock=lambda:now[0]
        values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id']);now[0]=221.
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.runtime.sends,0)

    async def test_auto_timeout_remains_uncertain_after_late_completion(self):
        self.safe();values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id'])
        self.core.call_seconds=.03;self.runtime.finish_gate.clear()
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.core.status(values[0])['state'],'UNCERTAIN')
        self.runtime.finish_gate.set();await self.core.drain_fixtures()
        self.assertEqual(self.core.status(values[0])['state'],'UNCERTAIN')
        self.assertEqual(self.runtime.sends,1)

    async def test_fake_cli_mode_trust_call_and_audit(self):
        with TemporaryDirectory(prefix='local-ai-policy-cli-test-') as folder:
            command=[sys.executable,'-m','agent_poc.approval_cli','--policy',str(Path(folder)/'cli.sqlite3')]
            result=await asyncio.to_thread(subprocess.run,command,
                input='show\nmode AUTO_ALLOW_SAFE enable\ntrust get_current_time - safe\ntools\ncall get_current_time\naudit\nquit\n',
                text=True,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('trusted_safe_scope',result.stdout)
            self.assertIn('operation_admitted',result.stdout)
            self.assertIn('COMPLETED',result.stdout)

    async def test_allow_all_non_readonly_executes_only_fake_runtime(self):
        from agent_poc.fake_runtime import FakeRuntime
        from mcp_manager.service import MCPService
        from mcp_manager.models import ServerConfig
        from agent_poc.mcp_catalog import MCPCatalog
        from agent_poc.service_approval import ServiceApprovalCore
        from agent_poc.service_execution import FakeServiceExecutor
        with TemporaryDirectory(prefix='local-ai-policy-counter-') as folder:
            base=Path(folder);runtime=FakeRuntime()
            service=None
            try:
                runtime.tools=[ToolInfo('fake_increment','Synthetic non-readonly counter',
                    {'type':'object','properties':{'value':{'type':'integer'}},'required':['value']},{'type':'object'})]
                profile=ServerConfig('counter',sys.executable)
                service=MCPService(database_path=base/'mcp.sqlite3',session_database_path=base/'session.sqlite3',
                    approved_configs=[profile],runtime_factory=lambda:runtime)
                sid=service.create_server('counter','Fake counter')['server_id'];service.start_server(sid)
                service.set_permission(sid,'fake_increment',True)
                catalog=MCPCatalog(service);entry=catalog.discover()[0]
                policy=ApprovalPolicy(base/'policy.sqlite3');policy.grant_trust(entry,verified_read_only=False)
                policy.set_mode('ALLOW_ALL',enable_auto=True)
                core=ServiceApprovalCore(catalog,FakeServiceExecutor(service),policy=policy)
                core.create_session('counter');rid=core.start_run('counter')
                p=core.issue(rid,'fake-counter',entry,{'value':2})
                self.assertEqual(core.apply_policy(rid,p['proposal_id']).action,'AUTO_APPROVE')
                await core.execute(rid,'fake-counter',entry,{'value':2})
                self.assertEqual(runtime.sends,1)
            finally:
                if service is not None:service.shutdown()
                else:runtime.shutdown()

    async def test_changed_root_contract_requires_new_trust(self):
        self.safe('read_file',self.root)
        with TemporaryDirectory(prefix='local-ai-policy-new-root-') as folder:
            root=Path(folder);(root/'test.txt').write_text('synthetic',encoding='utf-8')
            contracts=dict(self.policy._contracts)
            sid=self.ids['filesystem'];contracts[sid]=replace(contracts[sid],root=root)
            fresh=ApprovalPolicy(self.path,contracts=contracts)
            self.assertEqual(fresh.evaluate(self.entries['read_file'],{'path':'test.txt'}).action,'ASK')

    async def test_other_database_is_not_modified(self):
        path=self.path.with_name('not-policy.sqlite3')
        with sqlite3.connect(path) as db:db.execute('CREATE TABLE private_data(value TEXT)')
        # Close explicitly before Windows cleanup and inspect without altering it.
        db.close();before=path.read_bytes()
        with self.assertRaises(PolicyError):ApprovalPolicy(path)
        self.assertEqual(path.read_bytes(),before)

    async def test_late_audit_error_does_not_release_uncertain_run(self):
        self.safe();values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id'])
        self.core.call_seconds=.03;self.runtime.finish_gate.clear()
        with self.assertRaises(ApprovalError):await self.execute(values)
        original=self.policy.audit
        def fail(event,**data):
            if event=='operation_outcome':raise OSError('synthetic late audit error')
            return original(event,**data)
        self.policy.audit=fail
        try:
            self.runtime.finish_gate.set();await self.core.drain_fixtures()
        finally:self.policy.audit=original
        self.assertEqual(self.core.status(values[0])['state'],'UNCERTAIN')

    async def test_schema_invalid_arguments_are_blocked_by_policy_too(self):
        self.safe('read_file',self.root)
        self.assertEqual(self.policy.evaluate(self.entries['read_file'],{'path':12}).action,'BLOCK')

    async def test_policy_storage_corruption_fails_closed(self):
        self.safe();values=self.prepare();self.core.apply_policy(values[0],values[1]['proposal_id'])
        db=sqlite3.connect(self.path)
        try:db.execute("UPDATE policy SET settings='{}' WHERE id=1");db.commit()
        finally:db.close()
        with self.assertRaises(ApprovalError):await self.execute(values)
        self.assertEqual(self.runtime.sends,0)
