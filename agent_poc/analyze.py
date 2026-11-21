"""Fixture-specific scoring, separate from Agent routing/execution."""
import json
import re
import statistics
from pathlib import Path
from .fixtures import approved_call

OUT=Path(__file__).resolve().parents[1]/'data'/'agent_poc'

def stat(values):
    return {'n':len(values),'mean':statistics.mean(values),'median':statistics.median(values),
            'min':min(values),'max':max(values)} if values else None

def calls(row):
    if not row['responses']:return []
    body=row['responses'][0]['body']
    return (body.get('choices') or [{}])[0].get('message',{}).get('tool_calls',[])

def score(row):
    q=row['question_id'];first=calls(row);text=row['final'] or ''
    allowed={'get_current_time'} if q in range(1,6) or q==0 else {'read_file','list_directory'} if q in (6,7,13) else {'get_current_time','read_file','list_directory'} if q==8 else {'convert_temperature'} if q in (11,12) else set()
    names=[c.get('function',{}).get('name') for c in first]
    selected=bool(names) and all(n in allowed for n in names)
    args=[]
    for call in first:
        try:args.append(approved_call(call['function']['name'],json.loads(call['function']['arguments'])))
        except (ValueError,KeyError,TypeError):args.append(False)
    executed={x['tool'] for x in row['ledger']}
    timeok=('get_current_time' in executed and not re.search(r'오후|\bPM\b|21\s*[:시]',text,re.I)
            and bool(re.search(r'(?:오전\s*9\s*시\s*30\s*분|0?9:30|9\s*시\s*30\s*분)',text)))
    dates=re.findall(r'(\d{4})\s*(?:년\s*|-)(\d{1,2})\s*(?:월\s*|-)(\d{1,2})',text)
    timeok=timeok and all(tuple(map(int,d))==(2026,11,17) for d in dates)
    fileok='read_file' in executed and '314159' in text
    good=(timeok if q in range(1,6) or q==0 else fileok if q in (6,7,13) else timeok and fileok if q==8 else
          'convert_temperature' in executed and ('77' in text if q==11 else '32' in text) if q in (11,12) else
          not first and bool(text))
    return {'selected':selected,'first_names':names,'arguments_ok':bool(args) and all(args),'correct':bool(good),
            'unnecessary':bool(names) and not allowed,'first_empty':bool(row['responses']) and not names and not row['responses'][0]['body'].get('choices',[{}])[0].get('message',{}).get('content'),
            'time_correct':bool(timeok),'file_correct':bool(fileok)}

def summarize(rows):
    out={}
    for model in sorted(set(r['model'] for r in rows)):
        rr=[r for r in rows if r['model']==model]
        per={}
        for q in sorted(set(r['question_id'] for r in rr)):
            data=[r for r in rr if r['question_id']==q];ss=[score(r) for r in data]
            per[q]={'n':len(data),'selected':sum(s['selected'] for s in ss),'arguments_ok':sum(s['arguments_ok'] for s in ss),
                    'correct':sum(s['correct'] for s in ss),'errors':sum(r['error'] is not None for r in data),
                    'executed_cases':sum(bool(r['ledger']) for r in data)}
        warm=[r for r in rr if not (r['repeat']==1 and r['question_id']==min(x['question_id'] for x in rr))]
        gpu=[];ctx=[];reported=[]
        for r in rr:
            gg=r['resources']['windows_gpu'];gg=[gg] if isinstance(gg,dict) else gg
            gpu.extend(g['DedicatedUsage'] for g in gg if 'DedicatedUsage' in g)
            for m in r['resources']['ps'].get('models',[]):
                if m['name']==model:ctx.append(m['context_length']);reported.append(m['size_vram'])
        out[model]={'n':len(rr),'per_question':per,'errors':[r['error'] for r in rr if r['error']],
            'waiting':sum(r['waiting'] for r in rr),'empty_first':sum(score(r)['first_empty'] for r in rr),
            'unnecessary':sum(score(r)['unnecessary'] for r in rr),'network_violations':sum(len(r['network_violations']) for r in rr),
            'warm_first_seconds':stat([r['responses'][0]['seconds']-r['requests'][0]['seconds'] for r in warm if r['responses']]),
            'warm_total_seconds':stat([r['elapsed_seconds'] for r in warm]),
            'first_prompt_tokens':stat([r['responses'][0]['body'].get('usage',{}).get('prompt_tokens',0) for r in rr if r['responses']]),
            'first_completion_tokens':stat([r['responses'][0]['body'].get('usage',{}).get('completion_tokens',0) for r in rr if r['responses']]),
            'total_tokens':stat([sum(x['body'].get('usage',{}).get('total_tokens',0) for x in r['responses']) for r in rr]),
            'contexts':sorted(set(ctx)),'windows_vram_max':max(gpu) if gpu else None,'ollama_vram_max':max(reported) if reported else None,
            'han_prose':sum(bool(re.search('[\u3400-\u9fff\u3040-\u30ff]',re.sub(r'```.*?```','',r['final'] or '',flags=re.S))) for r in rr),
            'hangul_prose':sum(bool(re.search('[가-힣]',re.sub(r'```.*?```','',r['final'] or '',flags=re.S))) for r in rr)}
    return out

if __name__=='__main__':
    summaries={}
    for name in ['common','recommended','extension','smoke','deny']:
        path=OUT/(name+'.jsonl')
        if path.exists():summaries[name]=summarize([json.loads(x) for x in path.read_text(encoding='utf-8').splitlines()])
    (OUT/'summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summaries,ensure_ascii=True,indent=2))
