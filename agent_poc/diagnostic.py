"""One native request per model for API comparison; no function execution."""
import json
from .evaluate import OUT, api, MODELS

def run():
    first=json.loads((OUT/'smoke.jsonl').read_text(encoding='utf-8').splitlines()[0])['requests'][0]['body']
    rows=[]
    try:
        for model in MODELS:
            for other in MODELS:
                if other!=model:api('/api/generate',{'model':other,'keep_alive':0})
            payload={'model':model,'messages':first['messages'],'tools':first['tools'],
                'think':False,'stream':False,'options':{'num_ctx':4096,'num_predict':512,'temperature':.2,'top_p':.9,'top_k':40,'seed':1}}
            response=api('/api/chat',payload)
            rows.append({'model':model,'request':payload,'response':response,'ps':api('/api/ps')})
            print(model,'native calls',len(response.get('message',{}).get('tool_calls',[])),flush=True)
            api('/api/generate',{'model':model,'keep_alive':0})
    finally:
        for model in MODELS:api('/api/generate',{'model':model,'keep_alive':0})
        (OUT/'native-diagnostic.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':run()
