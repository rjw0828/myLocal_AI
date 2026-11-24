"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../static/js/agent-policy.js'),'utf8');
const flush = () => new Promise(setImmediate);
const deferred = () => { let resolve; const promise = new Promise(r => resolve=r); return {promise,resolve}; };
const packet = (session_id,mode='MANUAL') => ({ok:true,json:async()=>({session_id,mode,manual_generation:0})});
function fixture(initial='A') {
  const nodes={};
  for (const id of ['session-id','agent-policy-mode','agent-policy-save','agent-policy-refresh','agent-policy-status'])
    nodes[id]={value:'',disabled:false,textContent:'',handlers:{},addEventListener(e,f){this.handlers[e]=f;}};
  nodes['session-id'].value=initial;
  const records={A:'MANUAL',B:'ALLOW_ALL'}, calls=[];
  let override=null;
  vm.runInNewContext(source, {document:{getElementById:id=>nodes[id],addEventListener(e,f){if(e==='DOMContentLoaded')f();}},
    fetch:async(url,options)=>{
      calls.push({url,...options}); if(override)return override(url,options);
      const id=decodeURIComponent(url.split('/')[2]);
      if(options.method==='PUT')records[id]=JSON.parse(options.body).mode;
      return packet(id,records[id]);
    }});
  return {nodes,calls,records,mode:nodes['agent-policy-mode'],save:nodes['agent-policy-save'],
    handle:f=>{override=f;}, async select(id){nodes['session-id'].value=id;await nodes['session-id'].handlers.input();},
    refresh:()=>nodes['agent-policy-refresh'].handlers.click()};
}
const tests=[],test=(name,run)=>tests.push({name,run});
test('no session cannot save or request permissions',async()=>{
  const f=fixture('');await flush();assert.equal(f.calls.length,0);assert.equal(f.save.disabled,true);
});
test('session switch restores independent saved permission',async()=>{
  const f=fixture();await flush();assert.equal(f.mode.value,'MANUAL');
  await f.select('B');assert.equal(f.mode.value,'ALLOW_ALL');
  await f.select('A');assert.equal(f.mode.value,'MANUAL');
  assert.ok(f.calls.every(c=>c.headers['X-Local-AI-Control']==='web-console' && c.cache==='no-store'));
});
test('save writes only selected session and reads back without tool calls',async()=>{
  const f=fixture();await flush();f.mode.value='ALLOW_ALL';await f.save.handlers.click();
  assert.equal(f.records.A,'ALLOW_ALL');assert.equal(f.records.B,'ALLOW_ALL');
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  assert.ok(f.calls.every(c=>c.url.startsWith('/sessions/')));
});
test('duplicate saves and late old-session receipts cannot overwrite new session',async()=>{
  const f=fixture();await flush();const pending=deferred();
  f.handle((url,options)=>options.method==='PUT'?pending.promise:packet(url.split('/')[2],url.includes('/B/')?'MANUAL':'ALLOW_ALL'));
  f.mode.value='ALLOW_ALL';const saving=f.save.handlers.click();await f.save.handlers.click();
  assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  await f.select('B');pending.resolve(packet('A','ALLOW_ALL'));await saving;
  assert.equal(f.mode.value,'MANUAL');assert.equal(f.save.disabled,false);
});
test('A-B-A rejects stale refresh using generation, not just session id',async()=>{
  const f=fixture();await flush();const old=deferred();let reads=0;
  f.handle(url=>{const id=url.split('/')[2];return id==='A' && reads++===0?old.promise:packet(id,'MANUAL');});
  const stale=f.refresh();await f.select('B');await f.select('A');old.resolve(packet('A','ALLOW_ALL'));await stale;
  assert.equal(f.mode.value,'MANUAL');
});
test('lost save response reconciles with GET and malformed reads fail closed',async()=>{
  const f=fixture();await flush();f.handle((url,options)=>{
    if(options.method==='PUT')throw Error('lost response');return packet('A','ALLOW_ALL');
  });
  f.mode.value='ALLOW_ALL';await f.save.handlers.click();
  assert.equal(f.mode.value,'ALLOW_ALL');assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  f.handle(()=>packet('A','AUTO_ALLOW_SAFE'));await f.refresh();assert.equal(f.save.disabled,true);
});
(async()=>{let failures=0;for(const {name,run} of tests){try{await run();}catch(e){failures++;console.error(name,e);}}
  console.log(`${tests.length-failures}/${tests.length} Session Permission UI checks passed`);process.exitCode=failures?1:0;})();
