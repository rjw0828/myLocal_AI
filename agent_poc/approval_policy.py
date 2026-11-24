"""Local owner policy, separate from one-use MCP execution authorization.

SQLite transactions serialize policy edits and final operation admission. No
server discovery, process launch, Agent loop, approval token or tool execution.
"""
from contextlib import contextmanager, closing
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from mcp_manager.registry import canonical_json, fingerprint
from .readiness_gate import ReadOnlyScope, no_reparse, local_path


class PolicyError(PermissionError): pass


class ApprovalMode(str, Enum):
    ASK_EVERY_TIME='ASK_EVERY_TIME'
    AUTO_ALLOW_SAFE='AUTO_ALLOW_SAFE'
    ALLOW_ALL='ALLOW_ALL'


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    mode: str
    revision: str

    def to_dict(self): return asdict(self)


def contract_identity(scope):
    if scope is None: return None
    return fingerprint({'server_id':scope.server_id,'kind':scope.kind,'root':str(scope.root) if scope.root else None,
        'tools':sorted(scope.tools),'allow_relative':scope.allow_relative,'time_default':scope.time_default})


class ApprovalPolicy:
    def __init__(self, path, *, contracts=None, clock=time.time):
        path=local_path(path)
        protected=Path('F:/LLM/AI_Project/myLocalAI')
        if path==protected or path.is_relative_to(protected) or path.suffix!='.sqlite3':
            raise PolicyError('Use an explicit experimental policy SQLite file')
        for parent in (path,*path.parents):
            if parent.exists(): no_reparse(parent)
        path.parent.mkdir(parents=True,exist_ok=True);no_reparse(path.parent)
        self.path,self.clock=path,clock
        self._contracts=dict(contracts or {})
        if any(type(s) is not ReadOnlyScope or sid!=s.server_id for sid,s in self._contracts.items()):
            raise PolicyError('Contracts must be trusted immutable server scopes')
        if path.exists():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as existing:
                tables={r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'policy' not in tables or not tables <= {'policy','audit','admissions','sqlite_sequence'}:
                    raise PolicyError('Refusing to add policy tables to another database')
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL, revision INTEGER NOT NULL, settings TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS admissions (proposal_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE)')
            db.execute('INSERT OR IGNORE INTO policy VALUES(1,?,0,?)',(uuid4().hex,canonical_json({
                'project_mode':ApprovalMode.AUTO_ALLOW_SAFE.value,'auto_enabled':False,'overrides':{},'trusts':{}})))
            db.commit()

    @contextmanager
    def _transaction(self):
        # New connections permit owner-task and CLI threads, and observe another
        # process's committed revocation. Never silently fall back to RAM policy.
        no_reparse(self.path)
        db=sqlite3.connect(self.path,timeout=2)
        try:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT epoch,revision,settings FROM policy WHERE id=1').fetchone()
            if row is None: raise PolicyError('Policy record missing')
            state=json.loads(row[2])
            ApprovalMode(state['project_mode'])
            if type(state['auto_enabled']) is not bool or not isinstance(state['trusts'],dict):
                raise PolicyError('Invalid policy data')
            yield db,state,f'{row[0]}:{row[1]}'
            db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()

    def revision(self):
        with self._transaction() as (_,__,revision):return revision

    def snapshot(self):
        with self._transaction() as (_,state,revision):return {'revision':revision,**deepcopy(state)}

    def _audit(self,db,event,data):
        db.execute('INSERT INTO audit(at,event,payload) VALUES(?,?,?)',(self.clock(),event,canonical_json(data)))

    def audit(self,event,**data):
        with self._transaction() as (db,_,revision):self._audit(db,event,{'policy_revision':revision,**data})

    def audit_log(self):
        with self._transaction() as (db,_,__):
            return [{'id':r[0],'at':r[1],'event':r[2],**json.loads(r[3])}
                    for r in db.execute('SELECT id,at,event,payload FROM audit ORDER BY id')]

    def _edit(self,change,event,data):
        with self._transaction() as (db,state,revision):
            change(state)
            db.execute('UPDATE policy SET revision=revision+1,settings=? WHERE id=1',(canonical_json(state),))
            self._audit(db,event,{'previous_revision':revision,**data})

    def set_mode(self,mode,*,server_id=None,enable_auto=False):
        mode=ApprovalMode(mode)
        if type(enable_auto) is not bool:raise PolicyError('Explicit automatic enable flag required')
        def change(state):
            if server_id is None:state['project_mode']=mode.value
            else:
                if not isinstance(server_id,str) or not server_id:raise PolicyError('Invalid server ID')
                state['overrides'][server_id]=mode.value
            if enable_auto:state['auto_enabled']=True
        self._edit(change,'mode_changed',{'server_id':server_id,'mode':mode.value,'explicit_enable':enable_auto})

    def disable_auto(self):
        self._edit(lambda s:s.update(auto_enabled=False),'automatic_disabled',{})

    def clear_override(self,server_id):
        self._edit(lambda s:s['overrides'].pop(server_id,None),'override_removed',{'server_id':server_id})

    def grant_trust(self,entry,*,resource=None,verified_read_only=False,expires_at=None):
        # This is a local-owner API, not a model-callable function. Callers must
        # use a current server CatalogEntry and explicit owner-reviewed scope.
        if type(verified_read_only) is not bool:raise PolicyError('Explicit read-only review required')
        if not entry.descriptor.get('allowed') or fingerprint(entry.descriptor)!=entry.identity:
            raise PolicyError('Current allowed Catalog entry required')
        if expires_at is not None and (type(expires_at) not in (float,int) or not math.isfinite(expires_at) or expires_at<=self.clock()):
            raise PolicyError('Invalid trust expiration')
        scope=self._contracts.get(entry.server_id)
        kind=None;resource_path=None
        if scope is not None and scope.kind=='filesystem':
            if resource is None:raise PolicyError('Filesystem trust requires an explicit file/directory')
            path=local_path(resource);no_reparse(path);path=path.resolve(strict=True)
            if not path.is_relative_to(scope.root):raise PolicyError('Trust cannot expand the server Root')
            kind='directory' if path.is_dir() else 'file' if path.is_file() else None
            if kind is None:raise PolicyError('Unsupported resource')
            resource_path=str(path)
        elif resource is not None:raise PolicyError('No verified filesystem contract for this resource')
        tid=uuid4().hex
        record={'trust_id':tid,'server_id':entry.server_id,'tool_name':entry.original_name,
            'catalog_fingerprint':entry.identity,'schema_fingerprint':fingerprint(entry.descriptor['input_schema']),
            'source':entry.descriptor['source'],'contract':contract_identity(scope),
            'resource':resource_path,'resource_kind':kind,'verified_read_only':verified_read_only,'expires_at':expires_at}
        self._edit(lambda s:s['trusts'].update({tid:record}),'trust_granted',{'trust_id':tid,'server_id':entry.server_id,'tool':entry.original_name})
        return tid

    def revoke_trust(self,trust_id):
        self._edit(lambda s:s['trusts'].pop(trust_id,None),'trust_revoked',{'trust_id':trust_id})

    def set_server_options(self,server_id,*,root=None,timeout=30):
        if not isinstance(server_id,str) or not server_id:raise PolicyError('Invalid server ID')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300:
            raise PolicyError('Call wait timeout must be within 0..300 seconds')
        if root is not None:
            root=local_path(root)
            protected=Path('F:/LLM/AI_Project/myLocalAI')
            if root==protected or root.is_relative_to(protected):raise PolicyError('Operating workspace Root forbidden')
            no_reparse(root)
            if not root.is_dir():raise PolicyError('Root must be an existing directory')
            root=str(root.resolve(strict=True))
        self._edit(lambda s:s.setdefault('server_options',{}).update({server_id:{'root':root,'timeout':timeout}}),
            'server_options_changed',{'server_id':server_id})

    def bind_contract(self,server_id,scope):
        if scope is not None and (type(scope) is not ReadOnlyScope or scope.server_id!=server_id):
            raise PolicyError('Explicit matching contract required')
        identity=contract_identity(scope)
        self._edit(lambda s:s.setdefault('contract_bindings',{}).update({server_id:identity}),
            'execution_contract_bound',{'server_id':server_id,'contract':identity})
        if scope is None:self._contracts.pop(server_id,None)
        else:self._contracts[server_id]=scope

    def invalidate_server(self,server_id,*,reason='server_stopped'):
        self._edit(lambda s:None,reason,{'server_id':server_id})

    def forget_server(self,server_id):
        def change(state):
            for key in ('server_options','contract_bindings','overrides'):state.get(key,{}).pop(server_id,None)
            state['trusts']={tid:r for tid,r in state['trusts'].items() if r['server_id']!=server_id}
        self._edit(change,'server_removed',{'server_id':server_id});self._contracts.pop(server_id,None)

    def _scope(self,entry,args):
        scope=self._contracts.get(entry.server_id)
        verifier=getattr(self,'contract_verifier',None)
        if verifier is not None:scope=verifier(entry)
        if scope is not None:scope.validate(entry.original_name,args,entry.descriptor['input_schema'])
        elif 'path' in args:raise PolicyError('Filesystem-like arguments need a verified Root contract')
        return scope

    def _evaluate(self,state,revision,entry,args):
        mode=ApprovalMode(state['overrides'].get(entry.server_id,state['project_mode']))
        def decision(action,reason):return PolicyDecision(action,reason,mode.value,revision)
        bindings=state.get('contract_bindings',{})
        if entry.server_id in bindings and bindings[entry.server_id]!=contract_identity(self._contracts.get(entry.server_id)):
            return decision('BLOCK','execution_contract_changed')
        options=state.get('server_options',{}).get(entry.server_id)
        if options is not None:
            contract=self._contracts.get(entry.server_id)
            if str(options['root'] or '')!=str(contract.root if contract is not None and contract.root else ''):
                return decision('BLOCK','execution_root_unverified_or_changed')
        try:
            if fingerprint(entry.descriptor)!=entry.identity:return decision('BLOCK','catalog_entry_invalid')
            Draft202012Validator(entry.descriptor['input_schema'],registry=Registry(),format_checker=FormatChecker()).validate(args)
        except Exception:return decision('BLOCK','arguments_schema_invalid')
        try:
            if not entry.descriptor['allowed']:return decision('BLOCK','tool_blocked')
            scope=self._scope(entry,args)
        except Exception:return decision('BLOCK','execution_scope_invalid')
        if mode is ApprovalMode.ASK_EVERY_TIME:return decision('ASK','ask_every_time')
        if not state['auto_enabled']:return decision('ASK','automatic_not_explicitly_enabled')
        matches=[r for r in state['trusts'].values()
                 if r['server_id']==entry.server_id and r['tool_name']==entry.original_name]
        if not matches:return decision('ASK','no_explicit_trust')
        for record in matches:
            if (record['catalog_fingerprint']!=entry.identity or record['source']!=entry.descriptor['source']
                    or record['schema_fingerprint']!=fingerprint(entry.descriptor['input_schema'])
                    or record['contract']!=contract_identity(scope)):
                continue
            if record['expires_at'] is not None and self.clock()>=record['expires_at']:continue
            if scope is not None and scope.kind=='filesystem':
                candidate=Path(args['path'])
                if not candidate.is_absolute():candidate=scope.root/candidate
                candidate=candidate.resolve(strict=True)
                trusted=Path(record['resource']);no_reparse(trusted)
                if (candidate!=trusted if record['resource_kind']=='file' else not candidate.is_relative_to(trusted)):
                    continue
            if mode is ApprovalMode.AUTO_ALLOW_SAFE and (not record['verified_read_only'] or scope is None):
                continue
            return decision('AUTO_APPROVE','trusted_safe_scope' if mode is ApprovalMode.AUTO_ALLOW_SAFE else 'explicit_allow_all_scope')
        return decision('ASK','trust_stale_expired_or_outside_scope')

    def evaluate(self,entry,args):
        with self._transaction() as (_,state,revision):return self._evaluate(state,revision,entry,args)

    def consume(self,proposal,entry,args,operation_id,consume):
        # Called at the existing final Core/service admission point. Configuration
        # updates in another instance cannot interleave this transaction.
        with self._transaction() as (db,state,revision):
            if proposal.get('policy_revision')!=revision:raise PolicyError('approval_policy_changed')
            decision=self._evaluate(state,revision,entry,args)
            if decision.action=='BLOCK':raise PolicyError(decision.reason)
            if proposal.get('approval_method')=='AUTO' and decision.action!='AUTO_APPROVE':
                raise PolicyError('automatic_trust_no_longer_valid')
            if proposal.get('approval_method') not in ('AUTO','MANUAL'):raise PolicyError('approval_method_missing')
            try:db.execute('INSERT INTO admissions VALUES(?,?)',(proposal['proposal_id'],operation_id))
            except sqlite3.IntegrityError:raise PolicyError('policy_duplicate_admission') from None
            consume()
            self._audit(db,'operation_admitted',{'policy_revision':revision,'run_id':proposal['run_id'],
                'proposal_id':proposal['proposal_id'],'operation_id':operation_id,'server_id':entry.server_id,
                'tool':entry.original_name,'arguments_fingerprint':fingerprint(args),'method':proposal['approval_method']})
