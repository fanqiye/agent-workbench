"""Model wire protocols. Cloud calls are opt-in and use named environment keys."""
import json
import os
import secrets
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Unavailable(RuntimeError):
    pass


def request(url, body=None, headers=None, timeout=90, method=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    req = Request(url, data=data, headers={'Content-Type':'application/json'} | (headers or {}), method=method)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise Unavailable('response exceeds size limit')
            return json.loads(raw)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise Unavailable(f'HTTP {code}') from None
    except (URLError, OSError, ValueError):
        raise Unavailable('network error or invalid JSON') from None


class Model:
    def __init__(self, config):
        self.config = config
        self.cloud_calls = 0

    def chat(self, messages, tools):
        cfg = self.config
        provider = cfg.get('provider','ollama')
        if provider == 'ollama':
            url = cfg.get('url','http://127.0.0.1:11434/api/chat')
            if urlsplit(url).hostname not in ('localhost','127.0.0.1'):
                raise ValueError('local model endpoint must be loopback')
            result = request(url, {'model':cfg['model'], 'messages':messages, 'tools':tools,
                                  'stream':False, 'keep_alive':'2m',
                                  'options':{'temperature':0,'num_ctx':8192,'num_predict':700}})
            msg = result['message']
            # Store actions and results, not an internal reasoning transcript.
            output = {'role':'assistant','content':msg.get('content','')}
            if msg.get('tool_calls'):
                output['tool_calls'] = msg['tool_calls']
            return output, result.get('prompt_eval_count',0)+result.get('eval_count',0)
        if not cfg.get('allow_cloud'):
            raise Unavailable('cloud evaluation disabled; configure a budget and opt in locally')
        limit=cfg.get('max_requests',0)
        if type(limit) is not int or not 1<=limit<=100 or self.cloud_calls>=limit:
            raise Unavailable('cloud request budget missing or exhausted')
        self.cloud_calls+=1
        key = os.environ.get(cfg.get('key_env',''), '')
        if not key:
            raise Unavailable('configured API key environment variable is missing')
        if provider in ('openai','mistral'):
            endpoints = {'openai':'https://api.openai.com/v1/chat/completions','mistral':'https://api.mistral.ai/v1/chat/completions'}
            wire = []
            pending = []
            for m in messages:
                if m['role'] == 'assistant' and m.get('tool_calls'):
                    calls=[]
                    for c in m['tool_calls']:
                        ident = c.get('id') or secrets.token_hex(8)
                        pending.append(ident)
                        calls.append({'id':ident,'type':'function','function':{'name':c['function']['name'], 'arguments':json.dumps(c['function']['arguments'])}})
                    wire.append({'role':'assistant','content':m.get('content') or None,'tool_calls':calls})
                elif m['role']=='tool':
                    wire.append({'role':'tool','tool_call_id':pending.pop(0),'content':m['content']})
                else:
                    wire.append(m)
            budget={'max_completion_tokens':700} if provider=='openai' else {'max_tokens':700}
            result = request(endpoints[provider], {'model':cfg['model'],'messages':wire,'tools':tools,**budget}, {'Authorization':'Bearer '+key})
            msg=result['choices'][0]['message']
            for call in msg.get('tool_calls',[]):
                call['function']['arguments']=json.loads(call['function']['arguments'])
            return {'role':'assistant','content':msg.get('content') or '', **({'tool_calls':msg['tool_calls']} if msg.get('tool_calls') else {})}, result.get('usage',{}).get('total_tokens',0)
        if provider == 'anthropic':
            wire=[]; pending=[]
            for m in messages:
                if m['role']=='system': continue
                if m['role']=='assistant':
                    blocks=[]
                    if m.get('content'): blocks.append({'type':'text','text':m['content']})
                    for c in m.get('tool_calls',[]):
                        ident=c.get('id') or secrets.token_hex(8); pending.append(ident)
                        blocks.append({'type':'tool_use','id':ident,'name':c['function']['name'],'input':c['function']['arguments']})
                    wire.append({'role':'assistant','content':blocks or [{'type':'text','text':'Continue'}]})
                elif m['role']=='tool':
                    block={'type':'tool_result','tool_use_id':pending.pop(0),'content':m['content']}
                    if wire and wire[-1]['role']=='user' and isinstance(wire[-1]['content'],list): wire[-1]['content'].append(block)
                    else: wire.append({'role':'user','content':[block]})
                else: wire.append(m)
            result=request('https://api.anthropic.com/v1/messages',{'model':cfg['model'],'max_tokens':700,'system':messages[0]['content'], 'messages':wire,
                           'tools':[{'name':t['function']['name'],'description':t['function']['description'],'input_schema':t['function']['parameters']} for t in tools]},
                           {'x-api-key':key,'anthropic-version':'2023-06-01'})
            calls=[{'id':b['id'],'function':{'name':b['name'],'arguments':b['input']}} for b in result['content'] if b['type']=='tool_use']
            return {'role':'assistant','content':'\n'.join(b['text'] for b in result['content'] if b['type']=='text'),**({'tool_calls':calls} if calls else {})},sum(result.get('usage',{}).get(k,0) for k in ('input_tokens','output_tokens'))
        if provider=='gemini':
            import re
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+',cfg['model']):raise ValueError('invalid model name')
            wire=[]
            for m in messages:
                if m['role']=='system':continue
                if m['role']=='tool':
                    part={'functionResponse':{'name':m['tool_name'],'response':{'result':json.loads(m['content'])}}}
                    if wire and wire[-1]['role']=='user' and 'functionResponse' in wire[-1]['parts'][0]:wire[-1]['parts'].append(part)
                    else:wire.append({'role':'user','parts':[part]})
                elif m['role']=='assistant':
                    parts=[{'text':m['content']}] if m.get('content') else []
                    parts += [c.get('gemini_part') or {'functionCall':{'name':c['function']['name'],'args':c['function']['arguments']}} for c in m.get('tool_calls',[])]
                    wire.append({'role':'model','parts':parts or [{'text':'Continue'}]})
                else:wire.append({'role':'user','parts':[{'text':m['content']}]})
            result=request('https://generativelanguage.googleapis.com/v1beta/models/'+cfg['model']+':generateContent',
                {'systemInstruction':{'parts':[{'text':messages[0]['content']}]},'contents':wire,
                 'tools':[{'functionDeclarations':[{'name':t['function']['name'],'description':t['function']['description'],'parametersJsonSchema':t['function']['parameters']} for t in tools]}],
                 'generationConfig':{'maxOutputTokens':700}}, {'x-goog-api-key':key})
            parts=result['candidates'][0]['content']['parts']
            calls=[{'function':{'name':p['functionCall']['name'],'arguments':p['functionCall'].get('args',{})},'gemini_part':p} for p in parts if 'functionCall' in p]
            return {'role':'assistant','content':'\n'.join(p['text'] for p in parts if 'text' in p and not p.get('thought')),**({'tool_calls':calls} if calls else {})},result.get('usageMetadata',{}).get('totalTokenCount',0)
        raise Unavailable('provider not implemented; no fallback to a different vendor')
