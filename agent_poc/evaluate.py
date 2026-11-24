"""Run explicitly approved virtual-fixture experiments, never real MCP tools.

python -m agent_poc.evaluate --suite common --approved-fixtures
"""
from .offline import install
install()
import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from urllib.request import Request,urlopen
from .runtime import Profile,evaluate,MODELS

OUT=Path(__file__).resolve().parents[1]/'data'/'agent_poc'
QUESTIONS=[
    '지금 몇 시야?','몇 시지?','몇 시일까?','몇 시더라?','현재 한국 시간을 알려줘.',
    'test.txt 파일 읽어줘.','test.txt를 읽어줘.','지금 몇 시인지 알려주고 test.txt도 읽어줘.',
    'Python으로 시간을 출력하는 코드를 작성해줘.','안녕?'
]

def api(path,data=None):
    req=Request('http://127.0.0.1:11434'+path,
                data=json.dumps(data).encode() if data is not None else None,
                headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=30) as f:return json.load(f)

def resources():
    # Only query GPU allocations for the active Ollama runner, not other apps.
    cmd="$ids=@(Get-Process -Name 'llama-server','ollama_llama_server' -ErrorAction SilentlyContinue | ForEach-Object {$_.Id}); Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory | Where-Object { $n=$_.Name; @($ids | Where-Object {$n -match ('pid_'+$_+'_')}).Count -gt 0 } | Select-Object Name,DedicatedUsage,SharedUsage | ConvertTo-Json -Compress"
    try:
        win=json.loads(subprocess.check_output(['powershell','-NoProfile','-Command',cmd],text=True).strip() or '[]')
    except Exception as exc:win={'error':type(exc).__name__}
    return {'ps':api('/api/ps'),'windows_gpu':win}

async def run(args):
    OUT.mkdir(parents=True,exist_ok=True)
    target=OUT/(args.suite+'.jsonl')
    if target.exists():raise SystemExit('Existing result file: refusing to repeat requests automatically')
    manifest={'models':MODELS,'ollama':api('/api/version'),'installed_models':api('/api/tags'),
              'suite':args.suite,'approved_fixtures':args.approved_fixtures,
              'context_target':4096,'context_limitation':'OpenAI compatibility API ignores num_ctx; record actual ps context',
              'thinking':'reasoning_effort=none','external_telemetry':False}
    (OUT/(args.suite+'-manifest.json')).write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    if args.suite=='common':cases=list(enumerate(QUESTIONS,1));repeats=5
    elif args.suite=='recommended':cases=[(q,QUESTIONS[q-1]) for q in (1,6,8)];repeats=5
    elif args.suite=='extension':cases=[(11,'섭씨 25도를 화씨로 환산해 줘.'),(12,'섭씨 0도는 화씨로 몇 도인가요?'),
        (13,'작업 폴더를 먼저 살펴보고 test.txt의 숫자를 알려 주세요.')];repeats=5
    else:cases=[(0,'get_current_time 도구로 서울 현재 시각을 확인해 줘.')];repeats=1
    try:
        for model in ([args.model] if args.model else MODELS):
            other=next(m for m in MODELS if m!=model)
            api('/api/generate',{'model':other,'keep_alive':0})
            for repeat in range(1,repeats+1):
                for q,question in cases:
                    decision='fixture-approved' if args.approved_fixtures else 'wait'
                    if args.suite=='deny':decision='deny'
                    row=await evaluate(question,Profile(model,'recommended' if args.suite=='recommended' else 'common'),
                        repeat,decision=decision,extension=args.suite=='extension')
                    row.update(question_id=q,repeat=repeat,resources=resources())
                    with target.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
                    print(model,repeat,q,'calls',len(row['ledger']),'final',bool(row['final']),'error',row['error'],flush=True)
                    if row['network_violations']:raise RuntimeError('Offline acceptance violation')
            api('/api/generate',{'model':model,'keep_alive':0})
    finally:
        # Runtime release only; no installed model/template/settings are changed.
        for model in MODELS:api('/api/generate',{'model':model,'keep_alive':0})

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',choices=['common','recommended','extension','smoke','deny'],default='smoke')
    parser.add_argument('--approved-fixtures',action='store_true',help='Only after user review of the fixed virtual-fixture scope')
    parser.add_argument('--model',choices=MODELS,help='Select a model without modifying the Agent code')
    asyncio.run(run(parser.parse_args()))
