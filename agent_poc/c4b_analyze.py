"""Evaluation-only scoring; never used to select, route or execute a tool."""
import json
from pathlib import Path, PureWindowsPath
import statistics
from jsonschema import Draft202012Validator
from .c4b_selection import OUT
from .c4_r1 import ROOT


def score(row, catalog):
    responses=row['responses']
    message=responses[0]['body']['choices'][0]['message'] if responses else {}
    calls=message.get('tool_calls') or []
    names=[];schema=[];scope=[];arguments=[]
    for call in calls:
        function=call['function']; entry=catalog.get(function['name'])
        names.append(entry['original'] if entry else 'unknown')
        try:
            args=json.loads(function['arguments']); arguments.append(args)
            schema.append(bool(entry) and not list(Draft202012Validator(entry['descriptor']['input_schema']).iter_errors(args)))
            if entry and entry['original']=='get_current_time':
                scope.append(args=={'timezone':'Asia/Seoul'})
            elif entry and entry['original'] in ('list_directory','read_file'):
                expected=PureWindowsPath(str(ROOT))
                if entry['original']=='read_file': expected=expected/'test.txt'
                scope.append(set(args)=={'path'} and PureWindowsPath(args.get('path',''))==expected)
            else: scope.append(False)
        except (ValueError,TypeError,KeyError):
            schema.append(False);scope.append(False)
    q=row['question_id']
    permitted=({'get_current_time'} if q<=5 else {'read_file','list_directory'} if q<=7 else
               {'get_current_time','read_file','list_directory'} if q==8 else set())
    return {'names':names,'arguments':arguments,'first_relevant_selection':bool(calls) and all(n in permitted for n in names),
        'schema_valid_calls':sum(schema),'exact_scope_calls':sum(scope),'calls':len(calls),
        'all_args_executable':bool(calls) and all(scope),
        'unnecessary':bool(calls) and not permitted,
        'empty':not calls and not message.get('content'),
        'first_text':message.get('content'),
        'no_tool_lookups':bool(permitted) and not calls}


def main():
    rows=[json.loads(line) for line in (OUT/'selection.jsonl').read_text(encoding='utf-8').splitlines()]
    catalog={e['alias']:e for e in json.loads((OUT/'selection-catalog.json').read_text(encoding='utf-8'))}
    result={}
    for model in sorted({r['model'] for r in rows}):
        rr=[r for r in rows if r['model']==model];per={}
        for row in rr: per.setdefault(str(row['question_id']),[]).append(score(row,catalog))
        scores=[score(r,catalog) for r in rr]
        latencies=[r['elapsed_seconds'] for r in rr]
        usage=[r['responses'][0]['body'].get('usage',{}) for r in rr if r['responses']]
        result[model]={'n':len(rr),'per_question':per,
            'lookup_questions':sum(r['question_id']<=8 for r in rr),
            'first_relevant_selection':sum(s['first_relevant_selection'] for s in scores),
            'calls':sum(s['calls'] for s in scores),'schema_valid_calls':sum(s['schema_valid_calls'] for s in scores),
            'exact_scope_calls':sum(s['exact_scope_calls'] for s in scores),
            'empty':sum(s['empty'] for s in scores),'unnecessary':sum(s['unnecessary'] for s in scores),
            'errors':sum(r['error'] is not None for r in rr),
            'mean_seconds':statistics.mean(latencies),'median_seconds':statistics.median(latencies),
            'max_seconds':max(latencies),'cold_first_seconds':latencies[0],
            'warm_mean_seconds':statistics.mean(latencies[1:]),
            'prompt_tokens':sum(u.get('prompt_tokens',0) for u in usage),
            'completion_tokens':sum(u.get('completion_tokens',0) for u in usage),
            'context_lengths':sorted({m['context_length'] for r in rr for m in r['ps']['models']}),
            'ollama_size_vram_bytes':sorted({m['size_vram'] for r in rr for m in r['ps']['models']})}
    (OUT/'selection-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    for model,summary in result.items():
        print(model,json.dumps({k:v for k,v in summary.items() if k!='per_question'}))
        print('per_question',[(q,sum(s['first_relevant_selection'] for s in ss),sum(s['all_args_executable'] for s in ss))
                             for q,ss in summary['per_question'].items()])


if __name__=='__main__': main()
