"""Bounded answer checks over synthetic provenance; no model, network or process."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from agent_poc.execution_evidence import (requirements, collect, satisfied,
    time_answer_consistent, check_answer, Requirement)
from agent_poc.mcp_catalog import CatalogEntry
from agent_poc.web_contract import ToolChatError
from mcp_manager.registry import canonical_json, fingerprint


class CoreFixture:
    def __init__(self):
        self._operations = {}; self._calls = {}; self.proposals = {}; self.operations = {}

    def add(self, tool, arguments, text, *, sid='server', root=None):
        oid = 'op-' + str(len(self._operations)); pid = 'p-' + oid
        schema = {'type':'object','properties':{'timezone':{'type':'string','default':'Asia/Seoul'}}}
        descriptor = {'name':tool, 'input_schema':schema, 'source':{
            'config_hash':'config','connection_generation':1,'permission_generation':1}}
        entry = CatalogEntry(sid, tool, sid+'_'+tool, canonical_json(descriptor), fingerprint(descriptor))
        self._operations[oid] = {'run_id':'run', 'proposal_id':pid}
        self._calls['run', pid] = pid, entry
        self.proposals[pid] = {'state':'CONSUMED','run_id':'run','session_id':'session','revision':1,
            'catalog_fingerprint':entry.identity,'tool_call_id':pid,'arguments_json':canonical_json(arguments),
            'arguments_fingerprint':fingerprint(arguments)}
        self.operations[oid] = {'operation_id':oid,'server_id':sid,'state':'COMPLETED','unsettled':False,
            'sent':True,'accepted':True,'proposal_consumed':True,'service_token_consumed':True,
            'result':{'is_error':False,'content':[{'type':'text','text':text}],'structured_content':None}}
        return entry

    def operation(self, oid): return deepcopy(self.operations[oid])
    def proposal(self, rid, pid): return deepcopy(self.proposals[pid])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.core = CoreFixture()
        self.entry = self.core.add('get_current_time', {'timezone':'Asia/Seoul'},
            '{"datetime":"2026-11-20T09:30:00+09:00"}')
        self.entries = [self.entry]; self.roots = {}
        self.ids = dict(job_id='job', session_id='session', revision=1)

    def proofs(self):
        return collect(self.core,'run',self.entries,self.roots,**self.ids)

    def check(self, question, answer):
        return check_answer(question,answer,self.core,'run',self.entries,self.roots,**self.ids)

    def covered(self, needed, evidence):
        return satisfied(needed,evidence,run_id='run',entries=self.entries,**self.ids)

    def test_general_explanations_code_and_prior_history_do_not_create_requirements(self):
        for question in ('안녕하세요.', '현재 시간을 구하는 Python 코드를 작성해줘.',
                'test.txt 읽기 예제 코드를 작성해줘.', '파일 읽는 방법을 알려줘.',
                '가상의 상황에서 현재 시간을 가정해줘.', '아까 내가 말한 시간이 몇 시였지?',
                '이전 대화의 test.txt 내용이 뭐였어?', '현재 시간 복잡도가 뭔가요?',
                'Translate "What time is it now?".', 'Read a file.', '파일 읽어줘.'):
            with self.subTest(question=question):
                self.assertEqual(requirements(question), ())
                self.assertEqual(self.check(question,'오후 11시 59분')['coverage'], 'not_applicable')

    def test_explicit_time_contradictions_and_precise_valid_answers(self):
        needed=requirements('현재 한국 시간을 알려줘.')
        for answer in ('오전 9시 30분입니다.', '09:30', '2026-11-20T00:30:00Z'):
            self.assertTrue(time_answer_consistent(answer,needed,self.proofs()), answer)
        for answer in ('오후 9시 30분', '09:31', '2026-11-21 오전 9시 30분',
                       '2026-11-20T09:30:01+09:00', '2026-99-20T09:30:00+09:00'):
            self.assertFalse(time_answer_consistent(answer,needed,self.proofs()), answer)

    def test_only_same_job_session_revision_run_and_source_satisfy(self):
        needed=requirements('현재 한국 시간을 알려줘.')
        proof=self.proofs()[0]
        for key, value in (('job_id','other'),('session_id','other'),('revision',2),('run_id','other'),
                ('server_id','other'),('source_identity','other'),('tool_name','convert_time')):
            with self.subTest(key=key):
                self.assertFalse(self.covered(needed,[replace(proof,**{key:value})]))

    def test_non_success_unconsumed_and_unknown_outcomes_are_not_evidence(self):
        original=deepcopy(self.core.operations['op-0'])
        for patch in ({'state':'FAILED'},{'state':'UNCERTAIN'},{'unsettled':True},
                {'sent':False},{'accepted':None},{'proposal_consumed':False},
                {'service_token_consumed':False},{'server_id':'other'},
                {'result':{'is_error':True,'content':[]}},
                {'result':{'is_error':False,'content':[]}}):
            self.core.operations['op-0']=original | patch
            self.assertEqual(self.proofs(), [], patch)
        self.core.operations['op-0']=original
        self.core.proposals['p-op-0']['state']='APPROVED'
        self.assertEqual(self.proofs(), [])

    def test_wrong_timezone_and_conversion_are_not_current_time_evidence(self):
        self.core.proposals['p-op-0']['arguments_json']='{"timezone":"UTC"}'
        self.core.proposals['p-op-0']['arguments_fingerprint']=fingerprint({'timezone':'UTC'})
        with self.assertRaisesRegex(ToolChatError,'tool_evidence_missing'):
            self.check('현재 한국 시간을 알려줘.','09:30')
        self.core=CoreFixture()
        self.entries=[self.core.add('convert_time',{},'2026-11-20T09:30:00+09:00')]
        self.assertEqual(self.proofs(), [])

    def test_complete_compound_coverage_and_wrong_file_target(self):
        root='C:\\fixture'
        self.entries.append(self.core.add('read_file',{'path':'test.txt'},'fixture',sid='fs'))
        self.roots={'fs':root}
        needed=requirements('현재 한국 시간과 test.txt 내용을 읽어서 알려줘.')
        self.assertEqual(len(needed),2)
        self.assertTrue(self.covered(needed,self.proofs()))
        self.assertFalse(self.covered(needed,self.proofs()[:1]))
        self.assertFalse(self.covered(requirements('other.txt 읽어줘.'),self.proofs()))
        self.assertEqual(self.check('현재 한국 시간과 test.txt 내용을 읽어서 알려줘.',
            'fixture at 09:30')['time_consistency'],'not_checked')

    def test_empty_file_is_valid_and_directory_is_not_read_evidence(self):
        self.core=CoreFixture();self.roots={'fs':'C:\\fixture'}
        self.entries=[self.core.add('read_file',{'path':'C:\\fixture\\test.txt'},'',sid='fs')]
        self.assertTrue(self.covered(requirements('test.txt 읽어줘.'),self.proofs()))
        self.core=CoreFixture()
        self.entries=[self.core.add('list_directory',{'path':'C:\\fixture'},'[FILE] test.txt',sid='fs')]
        self.assertFalse(self.covered(requirements('test.txt 읽어줘.'),self.proofs()))
        self.assertTrue(self.covered(requirements('작업 폴더의 파일 목록을 보여줘.'),self.proofs()))

    def test_ambiguous_roots_and_opaque_time_are_reported_unverified(self):
        self.roots={'a':'C:\\a','b':'C:\\b'}
        verdict=self.check('test.txt 읽어줘.','unverified')
        self.assertEqual(verdict['coverage'],'ambiguous')
        self.assertEqual(verdict['answer_semantics'],'not_verified')
        self.assertEqual(self.check('현재 한국 시간과 test.txt 내용을 읽어서 알려줘.',
            '09:30')['coverage'], 'ambiguous')
        self.core.operations['op-0']['result']['content'][0]['text']='opaque actual server result'
        verdict=self.check('현재 한국 시간을 알려줘.','오후 11시 59분')
        self.assertEqual(verdict['time_consistency'],'not_checked')

    def test_source_or_arguments_tamper_cannot_create_evidence(self):
        self.core.proposals['p-op-0']['arguments_json']='{"timezone":"UTC"}'
        self.assertEqual(self.proofs(), [])
        self.core.proposals['p-op-0']['arguments_fingerprint']=fingerprint({'timezone':'UTC'})
        self.core.proposals['p-op-0']['catalog_fingerprint']='other-source'
        self.assertEqual(self.proofs(), [])
