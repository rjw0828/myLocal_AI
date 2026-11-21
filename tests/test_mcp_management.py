"""Temporary management databases and memory owner transport only."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_poc.mcp_management import MCPManagement, ManagementError
from agent_poc.owner_runtime import OwnerFakeRuntime
from agent_poc.readiness_gate import ReadOnlyScope, aligned_read_only_scope
from agent_poc.approval_core import ApprovalError
from mcp_manager.models import ServerConfig, ToolInfo
from mcp_manager.registry import config_data, fingerprint


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=2

    def setUp(self):
        self.temp=TemporaryDirectory(prefix='local-ai-management-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'workspace';self.root.mkdir()
        (self.root/'test.txt').write_text('Fake fixture',encoding='utf-8')
        self.runtime=OwnerFakeRuntime(prepared=True)
        self.addCleanup(self.runtime.shutdown)  # Factory may never be invoked by a registration-only test.
        self.runtime.tools=[ToolInfo('get_current_time','Fake time',{'type':'object','properties':{
            'timezone':{'type':'string','default':'Asia/Seoul'}},'additionalProperties':False})]+[
            ToolInfo(name,'Fake filesystem',{'type':'object','properties':{'path':{'type':'string'}},
                'required':['path'],'additionalProperties':False}) for name in ('list_directory','read_file')]
        def bind(runtime,config,options):
            kind='filesystem' if options['root'] else 'time'
            scope=aligned_read_only_scope(ReadOnlyScope(config.server_id,kind,
                Path(options['root']) if options['root'] else None),config)
            runtime.manager._client_factory.bind(scope);return scope
        self.manager=MCPManagement(self.base/'management',runtime_factory=lambda:self.runtime,contract_binder=bind)
        self.addCleanup(self.manager.shutdown)

    async def asyncTearDown(self):
        self.runtime.dispatch_gate.set();self.runtime.finish_gate.set()
        for test in self.manager._tests.values():await test['core'].drain_fixtures()

    def add(self,kind='time',**kwargs):
        args=['-m','mcp_server_time','--local-timezone=Asia/Seoul'] if kind=='time' else [
            sys.executable,'--read-only','--confine-roots',str(self.root)]
        return self.manager.add_server(kind,sys.executable,args,root=self.root if kind=='filesystem' else None,
            confirm=kwargs.pop('confirm',lambda _:True),**kwargs)['server_id']

    def connect(self,kind='time'):
        sid=self.add(kind);self.manager.test_connection(sid,confirm=lambda _:True)
        return sid

    def ready(self,kind='time',tool=None):
        sid=self.connect(kind);name=tool or ('get_current_time' if kind=='time' else 'read_file')
        self.manager.set_permission(sid,name,True);return sid,name

    def safe(self,sid,name):
        resource=self.root if name!='get_current_time' else None
        self.manager.trust(sid,name,resource=resource,verified_read_only=True,confirm=lambda _:True)
        self.manager.set_mode('AUTO_ALLOW_SAFE',server_id=sid,enable_auto=True)

    async def test_registration_never_starts_and_no_permissions(self):
        sid=self.add();self.assertIsNone(self.manager._runtime)
        self.assertEqual(self.manager.details(sid)['status']['state'],'STOPPED')
        self.assertEqual(self.manager.service.permissions(sid),[])

    async def test_registration_decline_preserves_manifest(self):
        result=self.manager.add_server('no',sys.executable,[],confirm=lambda _:False)
        self.assertIsNone(result);self.assertFalse(self.manager.manifest.exists())
        self.assertEqual(self.manager.list_servers(),[])

    async def test_add_preserves_existing_profile_and_uses_registry(self):
        self.add();self.add('filesystem')
        self.assertEqual(len(json.loads(self.manager.manifest.read_text())['profiles']),2)
        self.assertEqual(len(self.manager.service.registry.list_servers()),2)

    async def test_duplicate_name_denied(self):
        self.add()
        with self.assertRaises(ManagementError):self.add()

    async def test_duplicate_execution_denied(self):
        self.add()
        with self.assertRaises(ManagementError):
            self.manager.add_server('other',sys.executable,['-m','mcp_server_time','--local-timezone=Asia/Seoul'],confirm=lambda _:True)

    async def test_invalid_executable(self):
        with self.assertRaises(ManagementError):
            self.manager.add_server('bad',str(self.base/'missing.exe'),[],confirm=lambda _:True)

    async def test_shell_launcher_rejected(self):
        import shutil
        with self.assertRaises(ValueError):
            self.manager.add_server('shell',shutil.which('cmd.exe'),[],confirm=lambda _:True)

    async def test_command_resolution_reviewed(self):
        reviews=[]
        with patch('agent_poc.mcp_management.shutil.which',return_value=sys.executable):
            self.manager.add_server('resolved','python',[],confirm=lambda r:reviews.append(r) or True)
        self.assertEqual(reviews[0]['executable'],str(Path(sys.executable).resolve()))
        self.assertEqual(reviews[0]['purpose'],'SAVE_PROFILE_ONLY')

    async def test_environment_secret_not_in_public_views_audit_or_repr(self):
        sid=self.add(env={'TOKEN':'sensitive-value'})
        self.assertNotIn('sensitive-value',str(self.manager.details(sid))+str(self.manager.audit()))
        config=self.manager.service.profiles()[self.manager.details(sid)['profile_id']]
        self.assertNotIn('sensitive-value',repr(config))
        self.assertEqual(dict(config.env),{'TOKEN':'sensitive-value'})
        self.manager.test_connection(sid,confirm=lambda r:'sensitive-value' not in str(r))
        self.assertEqual(dict(self.runtime.manager._configs[sid].env),{'TOKEN':'sensitive-value'})

    async def test_environment_validation(self):
        for env in ({'BAD=KEY':'x'},{'KEY':'a\x00b'},[('path','a'),('PATH','b')],None,['AB']):
            with self.subTest(env=env), self.assertRaises(ValueError):
                ServerConfig('x',sys.executable,(),env=env)

    async def test_legacy_config_hash_preserved_without_env(self):
        config=ServerConfig('x',sys.executable,())
        self.assertEqual(set(config_data(config)),{'executable','args','cwd'})
        self.assertNotEqual(fingerprint(config_data(config)),fingerprint(config_data(replace(config,env={'TOKEN':'x'}))))

    async def test_connection_decline_no_runtime(self):
        sid=self.add();result=self.manager.test_connection(sid,confirm=lambda _:False)
        self.assertFalse(result['started']);self.assertIsNone(self.manager._runtime)

    async def test_connection_discovery_no_automatic_allow_or_execute(self):
        sid=self.connect();self.assertTrue(self.manager.tools(sid))
        self.assertTrue(all(not t['allowed'] for t in self.manager.tools(sid)))
        self.assertEqual(self.runtime.sends,0)

    async def test_start_failure_stops_no_retry(self):
        sid=self.add()
        with patch.object(self.manager.service,'start_server',side_effect=RuntimeError('secret')) as start:
            with self.assertRaisesRegex(ManagementError,'Connection failed'):
                self.manager.test_connection(sid,confirm=lambda _:True)
        self.assertEqual(start.call_count,1);self.assertEqual(self.runtime.sends,0)

    async def test_ask_manual_allow_once(self):
        sid,name=self.ready();prepared=self.manager.prepare_tool_test(sid,name,{})
        self.assertEqual(prepared['decision']['action'],'ASK');self.assertEqual(self.runtime.sends,0)
        result=await self.manager.run_tool_test(prepared['test_id'],confirm=lambda _:True)
        self.assertEqual(result['run']['state'],'COMPLETED');self.assertEqual(self.runtime.sends,1)

    async def test_deny_no_send(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        result=await self.manager.run_tool_test(p['test_id'],confirm=lambda _:False)
        self.assertEqual(result['run']['error'],'approval_denied');self.assertEqual(self.runtime.sends,0)

    async def test_safe_time_and_explicit_test_consent(self):
        sid,name=self.ready();self.safe(sid,name)
        p=self.manager.prepare_tool_test(sid,name,{})
        self.assertEqual(p['decision']['action'],'AUTO_APPROVE');self.assertEqual(self.runtime.sends,0)
        result=await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(result['run']['state'],'COMPLETED')
        self.assertTrue(any(r['event']=='operation_admitted' and r['method']=='AUTO' for r in self.manager.audit()))

    async def test_auto_test_decline_still_no_send(self):
        sid,name=self.ready();self.safe(sid,name);p=self.manager.prepare_tool_test(sid,name,{})
        await self.manager.run_tool_test(p['test_id'],confirm=lambda _:False)
        self.assertEqual(self.runtime.sends,0)

    async def test_safe_filesystem_relative_read(self):
        sid,name=self.ready('filesystem');self.safe(sid,name)
        p=self.manager.prepare_tool_test(sid,name,{'path':'test.txt'})
        self.assertEqual(p['decision']['action'],'AUTO_APPROVE')
        result=await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(result['run']['state'],'COMPLETED')

    async def test_outside_root_blocked(self):
        sid,name=self.ready('filesystem');self.safe(sid,name)
        self.assertEqual(self.manager.decision(sid,name,{'path':str(self.base/'outside.txt')})['action'],'BLOCK')

    async def test_traversal_blocked(self):
        sid,name=self.ready('filesystem')
        self.assertEqual(self.manager.decision(sid,name,{'path':'../outside.txt'})['action'],'BLOCK')

    async def test_unverified_new_tool_ask_no_special_agent_code(self):
        # Generic runtime adapter, no root-bearing contract or name dispatch.
        self.manager.shutdown()
        runtime=OwnerFakeRuntime();self.addCleanup(runtime.shutdown)
        self.manager=MCPManagement(self.base/'generic',runtime_factory=lambda:runtime)
        sid=self.manager.add_server('new',sys.executable,[],confirm=lambda _:True)['server_id']
        self.manager.test_connection(sid,confirm=lambda _:True)
        self.manager.set_permission(sid,'fixture_lookup',True)
        self.manager.set_mode('AUTO_ALLOW_SAFE',server_id=sid,enable_auto=True)
        self.assertEqual(self.manager.decision(sid,'fixture_lookup',{'value':5})['action'],'ASK')
        self.manager.trust(sid,'fixture_lookup',confirm=lambda _:True)
        self.manager.set_mode('ALLOW_ALL',server_id=sid,enable_auto=True)
        self.assertEqual(self.manager.decision(sid,'fixture_lookup',{'value':5})['action'],'AUTO_APPROVE')

    async def test_new_tool_not_inherited_by_trust(self):
        sid,name=self.ready('filesystem');self.safe(sid,name)
        self.manager.set_permission(sid,'list_directory',True)
        self.assertEqual(self.manager.decision(sid,'list_directory',{'path':'.'})['action'],'ASK')

    async def test_policy_change_invalidates_pending(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        self.manager.set_mode('ASK_EVERY_TIME')
        with self.assertRaises(ApprovalError):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(self.runtime.sends,0)

    async def test_schema_change_invalidates_pending(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        self.runtime.tools[0]=replace(self.runtime.tools[0],description='new contract')
        with self.assertRaises(Exception):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(self.runtime.sends,0)

    async def test_permission_revocation_invalidates(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        self.manager.set_permission(sid,name,False)
        with self.assertRaises(Exception):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(self.runtime.sends,0)

    async def test_restart_invalidates_trust(self):
        sid,name=self.ready();self.safe(sid,name)
        self.manager.stop_server(sid);self.manager.test_connection(sid,confirm=lambda _:True)
        self.assertEqual(self.manager.decision(sid,name,{})['action'],'ASK')

    async def test_revoke_trust_invalidates_request(self):
        sid,name=self.ready();self.safe(sid,name);p=self.manager.prepare_tool_test(sid,name,{})
        self.manager.revoke_trust(self.manager.details(sid)['trusts'][0]['trust_id'])
        with self.assertRaises(ApprovalError):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)

    async def test_duplicate_test_submission_blocked(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        with self.assertRaises(ManagementError):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(self.runtime.sends,1)

    async def test_expired_proposal_no_send(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        test=self.manager._tests[p['test_id']];test['core']._proposals[test['proposal']]['expires_at']=0
        with self.assertRaises(ApprovalError):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)
        self.assertEqual(self.runtime.sends,0)

    async def test_cancel_before_submission(self):
        sid,name=self.ready();p=self.manager.prepare_tool_test(sid,name,{})
        self.manager.cancel_test(p['test_id'])
        with self.assertRaises(ApprovalError):await self.manager.run_tool_test(p['test_id'],confirm=lambda _:True)

    async def test_live_delete_blocked_then_stopped_delete(self):
        sid=self.connect()
        with self.assertRaises(Exception):self.manager.delete_server(sid)
        self.manager.stop_server(sid);self.manager.delete_server(sid)
        self.assertEqual(self.manager.list_servers(),[])

    async def test_options_persist_no_automatic_override_enable(self):
        sid=self.add(timeout=12,mode='ALLOW_ALL')
        self.assertEqual(self.manager.details(sid)['options']['timeout'],12)
        self.assertFalse(self.manager.details(sid)['auto_enabled'])

    async def test_reopen_restores_registry_not_runtime(self):
        sid=self.add();self.manager.shutdown()
        self.manager=MCPManagement(self.base/'management',runtime_factory=lambda:self.runtime)
        self.assertEqual(self.manager.details(sid)['status']['state'],'STOPPED');self.assertIsNone(self.manager._runtime)

    async def test_environment_variables_never_select_operating_files(self):
        with patch.dict('os.environ',{'LOCAL_AI_MCP_APPROVALS':'F:/LLM/AI_Project/myLocalAI/data/secret.json',
                                     'LOCAL_AI_MCP_DB_PATH':'F:/LLM/AI_Project/myLocalAI/data/secret.sqlite3'}):
            sid=self.add();self.assertTrue(self.manager.details(sid)['execution_approved'])

    async def test_operating_base_denied_before_open(self):
        with self.assertRaises(ManagementError):
            MCPManagement(Path('F:/LLM/AI_Project/myLocalAI/data'),runtime_factory=lambda:self.runtime)

    async def test_invalid_root_and_timeout_before_persistence(self):
        for options in ({'root':self.base/'missing'},{'timeout':float('nan')},{'timeout':0}):
            with self.subTest(options=options),self.assertRaises(ManagementError):
                self.manager.add_server('invalid',sys.executable,[],confirm=lambda _:True,**options)
        self.assertFalse(self.manager.manifest.exists())

    async def test_current_permission_is_required_for_tool_test(self):
        sid=self.connect()
        with self.assertRaises(ManagementError):self.manager.prepare_tool_test(sid,'get_current_time',{})

    async def test_schema_validation_before_proposal(self):
        sid,name=self.ready()
        with self.assertRaises(Exception):self.manager.prepare_tool_test(sid,name,{'timezone':5})
        self.assertEqual(self.runtime.sends,0)

    async def test_same_names_on_different_servers_have_unique_aliases(self):
        a=self.connect()
        b=self.manager.add_server('second-time',sys.executable,
            ['-m','mcp_server_time','--local-timezone=Asia/Seoul'],env={'FIXTURE':'second'},confirm=lambda _:True)['server_id']
        self.manager.test_connection(b,confirm=lambda _:True)
        for sid in (a,b):self.manager.set_permission(sid,'get_current_time',True)
        entries=self.manager.catalog.discover()
        self.assertEqual(len({e.alias for e in entries}),2)

    async def test_root_change_blocks_even_fresh_test(self):
        sid,name=self.ready('filesystem');self.safe(sid,name)
        other=self.base/'other-root';other.mkdir()
        self.manager.policy.set_server_options(sid,root=other)
        self.assertEqual(self.manager.decision(sid,name,{'path':'test.txt'})['action'],'BLOCK')

    async def test_option_change_during_connection_consent_no_launch(self):
        sid=self.add()
        def consent(_):self.manager.policy.set_server_options(sid,timeout=10);return True
        with self.assertRaises(ManagementError):self.manager.test_connection(sid,confirm=consent)
        self.assertIsNone(self.manager._runtime)

    async def test_trust_consent_can_decline(self):
        sid,name=self.ready()
        self.assertIsNone(self.manager.trust(sid,name,confirm=lambda _:False))
        self.assertEqual(self.manager.details(sid)['trusts'],[])

    async def test_registration_options_failure_removes_registry_row(self):
        with patch.object(self.manager.policy,'set_server_options',side_effect=OSError()):
            with self.assertRaises(ManagementError):self.add()
        self.assertEqual(self.manager.list_servers(),[])
        self.assertEqual(len(json.loads(self.manager.manifest.read_text())['profiles']),1)

    async def test_finished_requests_do_not_fill_profile_queue(self):
        self.add();self.add('filesystem')
        self.assertEqual(list(self.manager.requests.directory.glob('*.json')),[])

    async def test_plain_repository_directory_rejected_for_secrets(self):
        with self.assertRaises(ManagementError):
            MCPManagement(Path(__file__).resolve().parents[1]/'unignored-settings',runtime_factory=lambda:self.runtime)

    async def test_unverified_root_cannot_be_a_tool_test(self):
        self.manager.shutdown();runtime=OwnerFakeRuntime();self.addCleanup(runtime.shutdown)
        self.manager=MCPManagement(self.base/'unverified',runtime_factory=lambda:runtime)
        sid=self.manager.add_server('generic',sys.executable,[],root=self.root,confirm=lambda _:True)['server_id']
        self.manager.test_connection(sid,confirm=lambda _:True)
        self.manager.set_permission(sid,'fixture_lookup',True)
        with self.assertRaises(ManagementError):self.manager.prepare_tool_test(sid,'fixture_lookup',{'value':1})

    async def test_cli_basic_schema_form_without_json(self):
        from agent_poc.management_cli import arguments_form
        with patch('builtins.input',side_effect=['test.txt','true','3','']):
            values=arguments_form({'type':'object','required':['path'],'properties':{
                'path':{'type':'string'},'enabled':{'type':'boolean'},'limit':{'type':'integer'},
                'optional':{'type':'string','default':'omitted'}}})
        self.assertEqual(values,{'path':'test.txt','enabled':True,'limit':3})

    async def test_fake_cli_register_list_and_quit(self):
        import subprocess
        base=self.base/'cli'
        # Add generic profile, then list/quit. Registration is owner-reviewed;
        # the Python CLI child must never spawn an MCP server.
        script='\n'.join(['2','demo',sys.executable,'','','','','','','y','1','0',''])
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','agent_poc.management_cli','--base',str(base)],
            input=script,capture_output=True,text=True,encoding='utf-8',env={**__import__('os').environ,'PYTHONIOENCODING':'utf-8'},timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('SAVE_PROFILE_ONLY',result.stdout)
        self.assertIn('STOPPED',result.stdout)

    async def test_fake_cli_connection_permission_test_and_stop(self):
        import os
        import subprocess
        sid=self.add()
        script='\n'.join(['4',sid,'y','5',sid,'fixture_lookup','allow',
            '8',sid,'fixture_lookup','5','y','10',sid,'0',''])
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','agent_poc.management_cli',
            '--base',str(self.manager.base)],input=script,capture_output=True,text=True,encoding='utf-8',
            env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('START_AND_DISCOVER_ONLY',result.stdout)
        self.assertIn('EXECUTE_EXACT_TOOL_ONCE',result.stdout)
        self.assertIn('COMPLETED',result.stdout)


if __name__=='__main__':unittest.main()
