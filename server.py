"""Trusted local operator UI. Browser mutations require a same-origin nonce."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs,urlsplit
import desk


def make_server(root, port=8805):
    nonce=secrets.token_urlsafe(32)
    # ponytail: one local inference worker; use a proper queue before any multi-user deployment.
    pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='local-agent')
    web=Path(__file__).parent/'web'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def reply(self,status,value,kind='application/json; charset=utf-8'):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            for key,val in [('Content-Type',kind),('Content-Length',str(len(raw))),('Cache-Control','no-store'),
                            ('X-Content-Type-Options','nosniff'),('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'")]:
                self.send_header(key,val)
            self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass

        def handle_request(self):
            host=self.headers.get('Host','');expected={'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)}
            if host not in expected:raise PermissionError('invalid host')
            route=urlsplit(self.path);params=parse_qs(route.query)
            if self.command=='GET':
                if route.path in ('/','/app.js','/style.css'):
                    filename={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[route.path]
                    kind={'/':'text/html; charset=utf-8','/app.js':'text/javascript','/style.css':'text/css'}[route.path]
                    self.reply(200,(web/filename).read_bytes(),kind);return
                if route.path=='/api/session':self.reply(200,{'nonce':nonce});return
                if route.path=='/healthz':self.reply(200,{'status':'ok'});return
                if route.path=='/api/runs':self.reply(200,desk.runs(root));return
                if route.path=='/api/run':self.reply(200,desk.get(root,params['id'][0]));return
                if route.path=='/api/export':
                    row=desk.get(root,params['id'][0])
                    if row['status']!='exported':raise ValueError('run has no approved export')
                    self.reply(200,(root/'exports'/row['state']['export']['filename']).read_bytes(),'text/calendar; charset=utf-8');return
                self.reply(404,{'error':'not found'});return
            if (self.headers.get('Origin') not in (None,'http://'+host)
                    or not secrets.compare_digest(self.headers.get('X-Workbench-Nonce',''),nonce)):
                raise PermissionError('same-origin session required')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                raise ValueError('expected bounded JSON body')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('expected object')
            if route.path=='/api/run':
                ident=desk.create(root,body['profile'],body['question'])
                pool.submit(desk.execute,root,ident);self.reply(202,{'id':ident})
            elif route.path=='/api/approve':self.reply(200,desk.approve(root,body['id'],body['reason']))
            elif route.path=='/api/cancel':desk.cancel(root,body['id']);self.reply(200,{'ok':True})
            else:self.reply(404,{'error':'not found'})

        def dispatch(self):
            try:self.handle_request()
            except PermissionError:self.reply(403,{'error':'本机来源校验失败，请重新打开页面。'})
            except (ValueError,KeyError,TypeError):self.reply(400,{'error':'请求或资料状态不符合要求；核对输入，资料变更后请重新运行。'})
            except (OSError,sqlite3.Error):self.reply(503,{'error':'本机存储暂不可用，请检查进程与运行目录。'})

        do_GET=dispatch
        do_POST=dispatch
    service=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    service.daemon_threads=True
    return service,pool


def serve(root):
    service,pool=make_server(root)
    print('Workbench: http://127.0.0.1:8805',flush=True)
    try:service.serve_forever()
    finally:
        service.server_close();pool.shutdown(wait=True,cancel_futures=True)
