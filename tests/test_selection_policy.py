"""Lookup classification and trusted execution envelopes; no real MCP/LLM."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import os
from tempfile import TemporaryDirectory
import unittest

from test_tool_calling import FakeMCP
from tool_calling.catalog import ToolCatalog
from tool_calling.filesystem_policy import FilesystemPolicy
from tool_calling.selection_policy import Requirement, applicable, execution_evidence, requirements, satisfied, time_answer_consistent


NON_LOOKUP = (
    "안녕하세요.", "1부터 10까지 더해줘.", "파이썬 리스트와 튜플 차이는?",
    "현재 시간을 구하는 Python 코드를 작성해줘.", "파일 읽는 방법을 알려줘.",
    "test.txt 읽기 예제 코드를 작성해줘.", "가상의 상황에서 현재 시간을 가정해줘.",
    "아까 내가 말한 시간이 몇 시였지?", "이전 대화의 test.txt 내용이 뭐였어?",
    "현재 파일 시스템 개념을 설명해줘.", "시간 복잡도가 뭔가요?",
    "'지금 몇 시야?'를 번역해줘.", "파일 읽지 말고 사용법을 설명해줘.",
    "What is a Python list?", "Write code to read test.txt.",
    "How do I read a file?", "Explain the current time API.",
    "Give an example of the current date.", "In a hypothetical situation, what time is it?",
    "What did I say earlier about the current time?", "Translate 'What time is it now?'.",
    "Do not read test.txt.", "Read a file.", "파일 읽어줘.",
)


class SelectionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.mcp = FakeMCP()
        self.mcp.contexts["time", "get_current_time"]["tool"]["input_schema"]["properties"]["timezone"]["default"] = "Asia/Seoul"
        self.catalog = ToolCatalog(self.mcp, {"time": ("time", None)}).discover()
        self.entry = next(iter(self.catalog.values()))

    def outcome(self, sid="time", text="2026-11-16T12:00:00+09:00"):
        return {"server_id": sid, "operation_id": "operation-1", "result": {
            "is_error": False, "content": [{"type": "text", "text": text}], "structured_content": None}}

    def proof(self, entry=None, arguments=None, outcome=None, **changes):
        return execution_evidence(entry or self.entry, arguments or {}, self.outcome() if outcome is None else outcome,
            **({"job_id": "job", "session_id": "session", "revision": 1} | changes))

    def check(self, needed, evidence, catalog=None):
        return satisfied(needed, evidence, job_id="job", session_id="session", revision=1,
                         catalog=self.catalog if catalog is None else catalog)

    def test_clear_current_time_requests(self):
        for q in ("지금 몇 시야?", "현재 한국 시간 알려줘.", "시간 도구를 사용해서 지금 몇 시인지 알려줘.",
                  "지금 날짜와 시간이 어떻게 돼?", "What time is it now?", "Show the current date."):
            with self.subTest(question=q):
                self.assertEqual([r.kind for r in requirements(q)], ["current_time"])

    def test_non_lookup_and_ambiguous_requests_are_not_forced(self):
        for q in NON_LOOKUP:
            with self.subTest(question=q): self.assertEqual(requirements(q), ())

    def test_file_read_targets_and_unicode(self):
        for q, path in (("test.txt 읽어줘.", "test.txt"), ("보고서.txt 내용을 보여줘.", "보고서.txt"),
                        ('Read "F:\\My Files\\note.txt".', "F:\\My Files\\note.txt")):
            with self.subTest(question=q):
                self.assertEqual(requirements(q), (Requirement("read_file", path),))

    def test_read_then_explain_is_still_a_lookup_and_time_in_filename_is_not_time(self):
        self.assertEqual(requirements("test.txt 읽고 설명해줘."), (Requirement("read_file", "test.txt"),))
        self.assertEqual(requirements("현재시간.txt 읽어줘."), (Requirement("read_file", "현재시간.txt"),))
        self.assertEqual(requirements("Read code.py and explain its contents."), (Requirement("read_file", "code.py"),))
        self.assertEqual(requirements("현재 시간 알려주고 설명해줘."), (Requirement("current_time"),))

    def test_directory_lookup_is_not_reading(self):
        self.assertEqual(requirements("작업 폴더에 어떤 파일이 있어?"), (Requirement("list_directory", "@workspace"),))
        self.assertEqual(requirements("notes 폴더 목록 보여줘."), (Requirement("list_directory", "notes"),))
        self.assertEqual(set(requirements('List folders "F:\\one" and "F:\\two".')),
                         {Requirement("list_directory", "F:\\one"), Requirement("list_directory", "F:\\two")})

    def test_multiple_requirements_and_files(self):
        needed = requirements("지금 시간과 a.txt 및 notes/b.txt 내용을 읽어줘.")
        self.assertEqual(set(needed), {Requirement("current_time"), Requirement("read_file", "a.txt"), Requirement("read_file", "notes/b.txt")})
        mixed = requirements('Read "F:\\root\\a.txt" and list folder "F:\\root".')
        self.assertEqual(set(mixed), {Requirement("read_file", "F:\\root\\a.txt"), Requirement("list_directory", "F:\\root")})
        self.assertEqual(set(requirements("지금 몇 시야? 그리고 a.txt 읽어줘.")),
                         {Requirement("current_time"), Requirement("read_file", "a.txt")})

    def test_timezone_is_not_inferred_from_relative_filename(self):
        self.assertEqual(requirements("지금 시간과 notes/readme.txt 내용을 읽어줘.")[0], Requirement("current_time"))
        self.assertEqual(requirements("지금 시간과 뉴욕.txt 내용을 읽어줘.")[0], Requirement("current_time"))
        self.assertEqual(requirements("현재 뉴욕 시간 알려줘."), (Requirement("current_time", "America/New_York"),))

    def test_claims_and_proposed_calls_are_not_evidence(self):
        for alleged in ({"message": {"content": "I used get_current_time. It is 12:00."}},
                        {"tool_calls": [{"name": "get_current_time"}]}, {}):
            self.assertIsNone(self.proof(outcome=alleged))
        self.assertFalse(self.check(requirements("지금 몇 시야?"), []))

    def test_successful_current_turn_execution_is_evidence(self):
        proof = self.proof()
        self.assertTrue(self.check(requirements("현재 한국 시간 알려줘."), [proof]))
        self.assertEqual(proof.timestamp, "2026-11-16T12:00:00+09:00")  # Parsed time only, no full result.

    def test_short_time_forms_and_noncurrent_exclusions(self):
        for q in ("지금 몇 시야?", "지금 몇시야?", "몇시니?", "몇시더라?", "현재 시간?", "지금 한국 시간은?"):
            with self.subTest(q=q): self.assertEqual(len(requirements(q)), 1)
        for q in ("시간을 출력하는 Python 코드 작성해줘", "옛날에는 시간을 어떻게 측정했어?",
                  "몇 시에 자는 게 좋아?", "오후 2시를 영어로 표현해줘"):
            with self.subTest(q=q): self.assertEqual(requirements(q), ())

    def test_particles_and_duplicate_windows_paths(self):
        absolute = r"F:\LLM\MCP_Workspace\test.txt"
        for q, expected in (("test.txt를 읽어줘", "test.txt"), ("test.txt 읽어줘", "test.txt"),
                            ("test.txt 내용 알려줘", "test.txt"), (absolute + "를 읽어줘", absolute),
                            (absolute + " 읽어줘", absolute), ("자료를.txt를 읽어줘", "자료를.txt"),
                            ('"test.txt를" 파일 읽어줘', "test.txt를"),
                            ('"F:\\root\\파일을" 파일 읽어줘', r"F:\root\파일을")):
            with self.subTest(q=q): self.assertEqual(requirements(q), (Requirement("read_file", expected),))
        self.assertEqual(len(requirements(f'"{absolute}" 및 {absolute}를 읽어줘')), 1)
        self.assertEqual(requirements("test.txt한글 읽어줘"), ())
        self.assertEqual(set(requirements(r"Read F:\root\a.txt and list folder F:\root")),
                         {Requirement("read_file", r"F:\root\a.txt"), Requirement("list_directory", r"F:\root")})

    def test_explicit_time_contradictions_and_valid_conversions(self):
        proof = self.proof(outcome=self.outcome(text=json.dumps({"timezone": "Asia/Seoul",
                              "datetime": "2026-11-16T02:49:31+09:00"})))
        needed = requirements("현재 한국 시간 알려줘")
        for answer in ("오전 2시 49분", "02:49", "2:49 AM", "2026년 11월 16일 오전 2시 49분 31초",
                       "2026-11-15T17:49:31Z"):
            with self.subTest(answer=answer): self.assertTrue(time_answer_consistent(answer, needed, [proof]))
        for answer in ("오후 2시 49분", "14:49", "2:49 PM", "오전 3시", "오전 2시 48분",
                       "2026-11-15", "2026-11-16T02:49:31Z", "오전 2시 49분 30초"):
            with self.subTest(answer=answer): self.assertFalse(time_answer_consistent(answer, needed, [proof]))

    def test_noon_midnight_date_and_uncheckable_scope(self):
        needed = (Requirement("current_time"),)
        for stamp, right, wrong in (("2026-11-16T00:00:00+09:00", "오전 12시", "오후 12시"),
                                     ("2026-11-16T12:00:00+09:00", "12:00 PM", "12:00 AM")):
            proof = self.proof(outcome=self.outcome(text=stamp))
            self.assertTrue(time_answer_consistent(right, needed, [proof]))
            self.assertFalse(time_answer_consistent(wrong, needed, [proof]))
        proof = self.proof()
        self.assertTrue(time_answer_consistent("두 시쯤입니다", needed, [proof]))  # Not certified accurate.
        self.assertTrue(time_answer_consistent("02:00", needed, [self.proof(outcome=self.outcome(text="opaque"))]))
        self.assertTrue(time_answer_consistent("02:00", needed, [proof, replace(proof, timestamp="2026-11-16T01:00:00+09:00")]))
        self.assertTrue(time_answer_consistent("file contains 02:00", needed + (Requirement("read_file", "a.txt"),), [proof]))

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_particle_request_evidence_matches_only_actual_target(self):
        with TemporaryDirectory() as folder:
            entries = self.fs_entries(folder)
            read = next(e for e in entries.values() if e["context"]["tool"]["name"] == "read_file")
            path = str(Path(folder) / "test.txt")
            proof = self.proof(read, {"path": path}, self.outcome("fs", "fixture"))
            self.assertTrue(self.check(requirements(path + "를 읽어줘"), [proof], entries))
            self.assertFalse(self.check(requirements(str(Path(folder) / "other.txt") + "를 읽어줘"), [proof], entries))

    def test_failed_empty_or_foreign_execution_not_evidence(self):
        bad = self.outcome(); bad["result"]["is_error"] = True
        for value in (bad, self.outcome("other"), self.outcome(text=""), self.outcome() | {"operation_id": ""}):
            with self.subTest(outcome=value): self.assertIsNone(self.proof(outcome=value))

    def test_old_job_session_revision_or_source_cannot_be_reused(self):
        proof = self.proof()
        for changed in (replace(proof, job_id="old"), replace(proof, session_id="other"),
                        replace(proof, revision=0), replace(proof, server_id="other"),
                        replace(proof, source_identity=("time", "changed", "conn", 0, "schema"))):
            self.assertFalse(self.check((Requirement("current_time"),), [changed]))

    def test_timezone_requirement_matches_executed_arguments(self):
        self.assertFalse(self.check(requirements("현재 뉴욕 시간 알려줘."), [self.proof()]))
        proof = self.proof(arguments={"timezone": "America/New_York"})
        self.assertTrue(self.check(requirements("현재 뉴욕 시간 알려줘."), [proof]))

    def test_convert_time_does_not_prove_a_current_time_lookup(self):
        entry = deepcopy(self.entry); entry["context"]["tool"]["name"] = "convert_time"
        self.assertIsNone(self.proof(entry=entry, arguments={"time": "12:00"}))

    def test_zero_offset_time_ago_can_prove_current_time(self):
        entry = deepcopy(self.entry); entry["context"]["tool"]["name"] = "get_time_ago"
        self.assertIsNotNone(self.proof(entry=entry))
        self.assertIsNone(self.proof(entry=entry, arguments={"hours_ago": 1}))

    def fs_entries(self, folder, sid="fs"):
        for name in ("read_file", "list_directory"):
            self.mcp.add(sid, name, {"type": "object"})
        policy = FilesystemPolicy([folder], verified=True, server_identity=(sid, "config", "connection-1"))
        return ToolCatalog(self.mcp, {sid: ("filesystem", policy)}).discover()

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_files_are_target_specific_and_listing_is_not_reading(self):
        with TemporaryDirectory() as folder:
            entries = self.fs_entries(folder)
            read = next(e for e in entries.values() if e["context"]["tool"]["name"] == "read_file")
            listing = next(e for e in entries.values() if e["context"]["tool"]["name"] == "list_directory")
            path = str(Path(folder) / "a.txt")
            proof = self.proof(read, {"path": path}, self.outcome("fs", ""))
            needed = requirements("a.txt 읽어줘.")
            self.assertTrue(self.check(needed, [proof], entries))  # Empty file is valid.
            self.assertFalse(self.check(requirements("b.txt 읽어줘."), [proof], entries))
            listing_proof = self.proof(listing, {"path": folder}, self.outcome("fs", "[FILE] a.txt"))
            self.assertFalse(self.check(needed, [listing_proof], entries))
            self.assertTrue(self.check(requirements("작업 폴더 목록 보여줘."), [listing_proof], entries))

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_multi_requirement_needs_every_success(self):
        with TemporaryDirectory() as folder:
            entries = self.fs_entries(folder) | self.catalog
            read = next(e for e in entries.values() if e["context"]["tool"]["name"] == "read_file")
            needed = requirements("지금 시간과 a.txt 내용을 알려줘.")
            # "내용 알려줘" is a read request, independent of the time request.
            self.assertEqual(len(needed), 2)
            time = self.proof(); file = self.proof(read, {"path": str(Path(folder)/"a.txt")}, self.outcome("fs", "file"))
            self.assertFalse(self.check(needed, [time], entries))
            self.assertTrue(self.check(needed, [time, file], entries))

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_multiple_roots_keep_only_unambiguous_requirements(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            entries = self.fs_entries(a, "a") | self.fs_entries(b, "b")
            self.assertEqual(applicable(requirements("test.txt 읽어줘."), entries), ())
            absolute = requirements(f'"{a}\\test.txt" 읽어줘.')
            self.assertEqual(applicable(absolute, entries), absolute)

    def test_missing_server_does_not_remove_clear_requirement(self):
        needed = requirements("test.txt 읽어줘.")
        self.assertEqual(applicable(needed, {}), needed)
        self.assertFalse(self.check(needed, [], {}))


if __name__ == "__main__":
    unittest.main()
