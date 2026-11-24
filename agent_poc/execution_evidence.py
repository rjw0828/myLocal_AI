"""Bounded pre-commit execution evidence, independent of model/Agent routing.

The conservative current-question classifier and explicit time-contradiction
checker preserve the legacy selection_policy contract (9516eb1). They choose no
tools, issue no approvals, and make no model or MCP requests. File contents,
weekdays, unparseable prose and mixed/multiple-time claims remain unverified.
"""
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import PureWindowsPath
import re

from mcp_manager.registry import fingerprint
from .web_contract import ToolChatError

_EXPLANATION = re.compile(
    r"예[제시]|가상|가정|코드|방법|사용법|뜻|번역|인용|개념|원리|문법|포맷|정의|표현|과거|이전|아까|지난|대화|기억|말고|"
    r"어떻게\s*(?:사용|구현|작성|만들|읽|확인|구하|구해|얻)|"
    r"쓰지\s*마|사용하지\s*마|알려주지\s*마|시간\s*(?:복잡도|표|제)|```|"
    r"\b(?:example|hypothetical|fictional|code|python|concept|syntax|format|definition|api|meaning|translate|quote|"
    r"previous|earlier|history|conversation|yesterday|instead|except)\b|"
    r"\bhow\s+(?:to|do|can)|\b(?:do not|don't)\b", re.I)
_TIME = re.compile(
    r"(?:지금|현재|오늘)\s*(?:(?:한국|서울|뉴욕|런던|도쿄|미국|UTC|KST)\s*)?"
    r"(?:몇\s*시|날짜|시각|시간)(?!표|복잡도|제|당|대별)|"
    r"\bwhat\s+(?:time|date)\s+is\s+it\b|"
    r"\b(?:current|today's)\s+(?:time|date)\b|\b(?:time|date)\s+now\b", re.I)
_SHORT_TIME = re.compile(r"^\s*몇\s*시(?:니|더라|야)(?:요)?[?？.!\s]*$")
_READ = re.compile(r"읽(?:어|고|기|을)|(?:내용|content).*(?:보여|알려|확인|show)|\bread\b|\bshow\b.*\bcontents?\b", re.I)
_LIST = re.compile(r"목록|어떤\s*파일|무슨\s*파일|\blist\b|\bwhat\s+files\b", re.I)
_DIRECTORY = re.compile(r"폴더|디렉[터토]리|작업\s*공간|\b(?:directory|directories|folders?|workspaces?)\b", re.I)
_QUOTED = re.compile(r'["`“”\']([^"`“”\'\n]+)["`“”\']')
_FILE = re.compile(r"(?<![\w./\\])(?:[A-Za-z]:[\\/])?(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,10}(?![A-Za-z0-9])")
_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/][^\s,;!?\"`<>]+")
_ZONES = (("Asia/Seoul", r"한국|서울|\b(?:Korea|Seoul|KST)\b"),
          ("America/New_York", r"뉴욕|\bNew York\b"),
          ("Europe/London", r"런던|\bLondon\b"),
          ("Asia/Tokyo", r"도쿄|\bTokyo\b"), ("UTC", r"\bUTC\b"))


def _file_targets(bare, *, extensionless=True):
    # Absolute tokens take precedence over extension matches inside them. Only
    # unquoted, ASCII-extension + particle + read-verb boundaries are separable.
    # Quoted names are handled verbatim by the caller; never strip a name stem.
    targets, spans = [], []
    for match in _ABSOLUTE.finditer(bare):
        value = match[0].rstrip(".,")
        particle = re.fullmatch(r"(.+\.[A-Za-z0-9]{1,10})(?:를|을|의|은|는|이|가)", value)
        if particle and re.match(r"\s+(?:읽|내용|보여|알려|확인)", bare[match.end():]):
            value = particle[1]
        if extensionless or _FILE.fullmatch(value):
            targets.append(value)
        spans.append(match.span())
    for match in _FILE.finditer(bare):
        if any(start < match.end() and match.start() < end for start, end in spans):
            continue
        tail = re.match(r"[^\s,;!?\"`<>]*", bare[match.end():])[0].rstrip(".")
        if tail and tail not in {"를", "을", "의", "은", "는", "이", "가"}:
            continue  # Ambiguous extension/name: do not invent a shortened path.
        targets.append(match[0])
    return targets


@dataclass(frozen=True)
class Requirement:
    kind: str
    target: str | None = None


def requirements(question):
    """Return only high-confidence current-turn requirements, never instructions."""
    if not isinstance(question, str):
        return ()
    file_masked = _FILE.sub(" FILE_PATH ", _QUOTED.sub(
        lambda match: " FILE_PATH " if _FILE.search(match[1]) else match[0], question))
    if _EXPLANATION.search(file_masked):
        return ()
    if re.search(r"설명|\bexplain\b", question, re.I) and not re.search(
            r"몇\s*시|알려|보여|읽(?:어|고)|\bread\b|\btell\b|\bshow\b|what\s+time\s+is\s+it", question, re.I):
        return ()
    result = []
    # Quoted phrases alone are not a live-time request; quoted file paths remain usable.
    if _TIME.search(_QUOTED.sub("", file_masked)) or _SHORT_TIME.fullmatch(question):
        zones = [zone for zone, pattern in _ZONES if re.search(pattern, file_masked, re.I)]
        explicit = re.findall(r"\b(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc)"
                              r"/[A-Za-z_]+(?:/[A-Za-z_]+)?(?![\w.])", file_masked)
        zones = list(dict.fromkeys(explicit + zones))
        result.extend(Requirement("current_time", zone) for zone in zones or [None])
    quoted = _QUOTED.findall(question)
    bare = _QUOTED.sub("", question)
    listing = bool(_LIST.search(question) and _DIRECTORY.search(question))
    targets = list(dict.fromkeys([p for p in quoted if _FILE.search(p)] + _file_targets(bare, extensionless=not listing)))
    wildcard = re.search(r"(?:^|[\s\"'`])[\w./\\:-]*[*?][\w./\\?*:-]*\.[A-Za-z0-9]", question)
    reading = bool(_READ.search(question)) and not wildcard
    if reading and listing and not targets:
        return tuple(result)  # Untyped mixed file/directory targets are ambiguous.
    if reading:
        # Explicit quoted files and absolute paths may have no extension (LICENSE).
        if not listing and re.search(r"파일|\bfile\b", question, re.I):
            targets.extend(p for p in quoted if p not in targets)
        unique = {str(PureWindowsPath(p)).casefold(): p for p in reversed(targets)}
        result.extend(Requirement("read_file", p) for p in reversed(list(unique.values())))
    if listing:
        # A mixed read/list request must not turn its file targets into directories.
        explicit_dirs = [p.rstrip(".,") for p in _ABSOLUTE.findall(bare)
                         if not reading or p.rstrip(".,") not in targets]
        explicit_dirs.extend(p for p in quoted if not reading or p not in targets)
        named = re.search(r"(?:^|\s)([\w./\\-]+)\s*(?:폴더|디렉[터토]리|folder|directory)", question, re.I)
        if not explicit_dirs:
            target = named[1] if named and named[1].lower() not in {"작업", "현재", "working", "workspace", "the", "어떤"} else "@workspace"
            explicit_dirs.append(target)
        result.extend(Requirement("list_directory", p) for p in explicit_dirs)
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class Evidence:
    job_id: str
    session_id: str
    revision: int
    run_id: str
    server_id: str
    operation_id: str
    source_identity: str
    tool_name: str
    kind: str
    target: str | None
    root: str | None
    timestamp: str | None = None

def _timestamp(text, structured):
    """Accept only explicit offset-aware ISO results, never infer from prose."""
    candidates = [structured] if isinstance(structured, dict) else []
    for value in text:
        try:
            candidates.append(json.loads(value))
        except (ValueError, TypeError):
            candidates.append(value)
    for value in candidates:
        raw = value.get("datetime") if isinstance(value, dict) else value
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.isoformat()
        except ValueError:
            pass
    return None


def time_answer_consistent(answer, needed, evidence):
    """Reject explicit contradictions only; this is not a truth/coverage verifier.

    Check pure time lookups with one distinct timestamp. Mixed file answers and
    multiple timezone/results are ambiguous without claim-to-source alignment.
    Unparseable prose/results are left unverified, never declared accurate.
    """
    if not needed or any(r.kind != "current_time" for r in needed):
        return True
    stamps = {e.timestamp for e in evidence if e.kind == "current_time" and e.timestamp}
    if len(stamps) != 1:
        return True
    expected = datetime.fromisoformat(stamps.pop())
    iso = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")
    for match in iso.finditer(answer):
        try:
            claim = datetime.fromisoformat(match[0].replace("Z", "+00:00"))
        except ValueError:
            return False
        precision = "second" if re.search(r"T\d{2}:\d{2}:\d{2}", match[0]) else "minute"
        reference = expected.astimezone(claim.tzinfo)
        if precision == "minute":
            claim, reference = claim.replace(second=0, microsecond=0), reference.replace(second=0, microsecond=0)
        if claim.replace(microsecond=0) != reference.replace(microsecond=0):
            return False
    remaining = iso.sub("", answer)
    for match in re.finditer(r"(\d{4})\s*(?:년\s*|-)(\d{1,2})\s*(?:월\s*|-)(\d{1,2})(?:\s*일)?", remaining):
        if tuple(map(int, match.groups())) != (expected.year, expected.month, expected.day):
            return False
    # No guessing about bare '2시': without AM/PM it can mean 02 or 14.
    pattern = re.compile(r"(?:(오전|오후|AM|PM)\s*)?(\d{1,2})\s*(?:시(?:\s*(\d{1,2})\s*분)?(?:\s*(\d{1,2})\s*초)?|:(\d{2})(?::(\d{2}))?)(?:\s*(AM|PM))?", re.I)
    for match in pattern.finditer(remaining):
        prefix, hour, korean_minute, korean_second, minute, second, suffix = match.groups()
        second = second or korean_second
        marker = (prefix or suffix or "").lower()
        hour = int(hour)
        if prefix and suffix and prefix.lower() != suffix.lower():
            return False
        if marker:
            if not 1 <= hour <= 12:
                return False
            hour = hour % 12 + (12 if marker in {"오후", "pm"} else 0)
        elif not minute and hour <= 12:
            continue
        if hour != expected.hour:
            return False
        if (minute or korean_minute) is not None and int(minute or korean_minute) != expected.minute:
            return False
        if second is not None and int(second) != expected.second:
            return False
    return True


def workspace_roots(manager, entries):
    """Capture attested roots from this Run's pinned catalog, not model text."""
    result = {}
    for entry in entries:
        scope = manager.verified_scope(entry)
        if scope is not None and scope.kind == 'filesystem':
            result[entry.server_id] = str(scope.root)
    return result


def collect(core, rid, entries, roots, *, job_id, session_id, revision):
    """Only consumed proposals bound to completed, settled service operations."""
    pinned = {(e.server_id, e.original_name, e.identity): e for e in entries}
    evidence = []
    for oid, binding in core._operations.items():
        if binding['run_id'] != rid:
            continue
        operation = core.operation(oid)
        proposal = core.proposal(rid, binding['proposal_id'])
        _, entry = core._calls[(rid, proposal['tool_call_id'])]
        if (proposal['state'] != 'CONSUMED'
                or proposal['session_id'] != session_id or proposal['revision'] != revision
                or proposal['run_id'] != rid or proposal['catalog_fingerprint'] != entry.identity
                or fingerprint(entry.descriptor) != entry.identity
                or (entry.server_id, entry.original_name, entry.identity) not in pinned
                or operation['operation_id'] != oid or operation['server_id'] != entry.server_id
                or operation['state'] != 'COMPLETED' or operation['unsettled']
                or operation['sent'] is not True or operation['accepted'] is not True
                or not operation['proposal_consumed'] or not operation['service_token_consumed']):
            continue
        result = operation.get('result')
        if not isinstance(result, dict) or result.get('is_error') is not False:
            continue
        content = result.get('content')
        if not isinstance(content, list):
            continue
        text = [block['text'] for block in content if isinstance(block, dict)
                and block.get('type') == 'text' and isinstance(block.get('text'), str)]
        structured = result.get('structured_content')
        if structured is not None and not isinstance(structured, dict):
            continue
        args = json.loads(proposal['arguments_json'])
        if fingerprint(args) != proposal['arguments_fingerprint']:
            continue
        name = entry.original_name
        kind = target = root = stamp = None
        if name == 'get_current_time' or (name == 'get_time_ago' and all(
                args.get(field, 0) == 0 for field in ('days_ago', 'hours_ago', 'minutes_ago', 'seconds_ago'))):
            if not any(text) and not structured:
                continue
            kind = 'current_time'
            target = args.get('timezone', entry.descriptor['input_schema'].get(
                'properties', {}).get('timezone', {}).get('default'))
            stamp = _timestamp(text, structured)
        elif name in {'read_file', 'list_directory'} and entry.server_id in roots:
            if (not text and not structured) or not isinstance(args.get('path'), str):
                continue
            kind, root = name, roots[entry.server_id]
            path = PureWindowsPath(args['path'])
            target = str(path if path.is_absolute() else PureWindowsPath(root) / path)
        if kind is not None:
            evidence.append(Evidence(job_id, session_id, revision, rid, entry.server_id,
                oid, entry.identity, name, kind, target, root, stamp))
    return evidence


def satisfied(needed, evidence, *, job_id, session_id, revision, run_id, entries):
    sources = {(e.server_id, e.identity, e.original_name) for e in entries}
    def matches(requirement, proof):
        if ((proof.job_id, proof.session_id, proof.revision, proof.run_id)
                != (job_id, session_id, revision, run_id)
                or proof.kind != requirement.kind
                or (proof.server_id, proof.source_identity, proof.tool_name) not in sources):
            return False
        if requirement.kind == 'current_time':
            return requirement.target is None or requirement.target == proof.target
        if not proof.target or not proof.root or not requirement.target:
            return False
        path = PureWindowsPath(proof.root if requirement.target == '@workspace' else requirement.target)
        if not path.is_absolute():
            if path.drive or path.root or '..' in path.parts:
                return False
            path = PureWindowsPath(proof.root) / path
        return path == PureWindowsPath(proof.target)
    return all(any(matches(requirement, proof) for proof in evidence) for requirement in needed)


def check_answer(question, answer, core, rid, entries, roots, *, job_id, session_id, revision):
    needed = original = requirements(question)
    if len({str(PureWindowsPath(root)).casefold() for root in roots.values()}) > 1:
        # Preserve legacy multi-root ambiguity handling without inventing a root.
        needed = tuple(r for r in needed if r.kind == 'current_time'
                       or r.target and PureWindowsPath(r.target).is_absolute())
    evidence = collect(core, rid, entries, roots, job_id=job_id, session_id=session_id, revision=revision)
    if not satisfied(needed, evidence, job_id=job_id, session_id=session_id,
                     revision=revision, run_id=rid, entries=entries):
        raise ToolChatError('tool_evidence_missing')
    if not time_answer_consistent(answer, needed, evidence):
        raise ToolChatError('tool_time_mismatch')
    time_checked = (bool(needed) and all(r.kind == 'current_time' for r in needed)
                    and len({e.timestamp for e in evidence if e.kind == 'current_time' and e.timestamp}) == 1)
    return {'coverage': 'ambiguous' if needed != original else 'matched' if needed else 'not_applicable',
            'time_consistency': 'no_explicit_contradiction' if time_checked else 'not_checked',
            'answer_semantics': 'not_verified'}
