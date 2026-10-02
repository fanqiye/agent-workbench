"""Single offline acceptance check. Scripted tools are not model-quality evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import desk
from tools import Knowledge,Analyst,Planner,safe_query,import_sales,agenda,valid_slot,calendar_bytes,validate,digest
from vendor import gateway
from vendor.providers import Unavailable


def expect(error, action):
    try:action()
    except error:return
    raise AssertionError('expected '+error.__name__)


def call(name,**arguments):
    return {'role':'assistant','content':'','tool_calls':[{'function':{'name':name,'arguments':arguments}}]},20


class ScriptedModel:
    """Uses deterministic choices to exercise execution contracts, not model intelligence."""
    def __init__(self,profile):self.profile=profile
    def chat(self,messages,tools):
        records=[(r['tool_name'],json.loads(r['content'])) for r in messages if r['role']=='tool']
        names=[r[0] for r in records]
        if self.profile=='knowledge':
            if not records:return call('search_docs',query='退款')
            if 'read_document' not in names:return call('read_document',id=records[-1][1]['results'][0]['id'])
            value=next(r[1] for r in records if r[0]=='read_document')
            return call('finish_answer',answer='按引用条款提交申请，由财务复核。',outcome='answer',
                        citations=[{'id':value['id'],'quote':'退款申请需提供订单号，由财务在两个工作日内复核。'}])
        if self.profile=='analyst':
            if not records:return call('describe_sales')
            queries=[r[1] for r in records if r[0]=='query_sales']
            if not queries:return call('query_sales',sql="SELECT month,SUM(revenue_cents)/100.0 AS revenue_yuan FROM sales GROUP BY month ORDER BY month")
            return call('finish_analysis',summary='以下表格列出三个账期的合成销售额，不据此推断原因。',query_ids=[q['query_id'] for q in queries])
        if not records:return call('read_agenda')
        data=records[0][1];selected={r[1]['task_id'] for r in records if r[0]=='select_slot'}
        remaining=[t for t in data['tasks'] if t['id'] not in selected]
        if not remaining:return call('propose_calendar')
        if records[-1][0]=='find_slots':
            slot=records[-1][1]['candidates'][0]
            return call('select_slot',task_id=slot['task_id'],start=slot['start'])
        return call('find_slots',task_id=remaining[0]['id'])


def main():
    checks=[]
    def passed(name):checks.append(name);print('PASS',name,flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);desk.initialize(root)
        cfg=json.loads((root/'config.json').read_text(encoding='utf-8'))
        states={}
        for profile in ('knowledge','analyst','planner'):
            ident=desk.create(root,profile,'Synthetic acceptance request')
            desk.execute(root,ident,ScriptedModel(profile));row=desk.get(root,ident)
            assert row['status']==('awaiting_approval' if profile=='planner' else 'completed'),row
            states[profile]=row
        passed('three_tool_loops_complete_with_scripted_models_not_quality_measurement')
        assert states['analyst']['state']['result']['tables'][0]['rows']==[['2026-06',180000.0],['2026-07',210000.0],['2026-08',186000.0]]
        comparison=Analyst(root,{},cfg).invoke('compare_revenue',{'earlier':'2026-07','later':'2026-08'})['verified_comparison']
        assert comparison['rows']==[['north','120000','90000','-30000','-25.00'],['south','90000','96000','6000','6.67'],['TOTAL','210000','186000','-24000','-11.43']]
        passed('integer_cents_aggregate_to_independent_expected_monthly_values')
        for sql in ["DROP TABLE sales","DELETE FROM sales","UPDATE sales SET orders=0","PRAGMA table_info(sales)",
                    "ATTACH DATABASE ':memory:' AS extra","SELECT * FROM sqlite_master","SELECT load_extension('anything')",
                    "SELECT randomblob(100000000)","SELECT 1; DROP TABLE sales",
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT max(x) FROM n",
                    "SELECT count(*) FROM sales a,sales b,sales c,sales d,sales e,sales f,sales g"]:
            expect(ValueError,lambda sql=sql:safe_query(root,sql))
        assert safe_query(root,'SELECT count(*) FROM sales')['rows']==[(12,)]
        passed('sql_writes_schema_pragmas_extensions_multiple_statements_and_work_budget_denied')
        source=root/'bad.csv';source.write_text('month,region,product,orders,revenue_cents,returns\n2026-08,north,desk,1,2,3\n',encoding='utf-8')
        expect(ValueError,lambda:import_sales(source,root/'sales.sqlite3'))
        assert safe_query(root,'SELECT count(*) FROM sales')['rows']==[(12,)]
        passed('invalid_sales_import_preserves_previous_dataset')
        knowledge=Knowledge(root,{},cfg)
        found=knowledge.invoke('search_docs',{'query':'退款'})['results'];assert [r['id'] for r in found]==['north-billing']
        expect(ValueError,lambda:knowledge.invoke('read_document',{'id':'south-billing'}))
        expect(ValueError,lambda:knowledge.invoke('read_document',{'id':'north-sso-admin'}))
        expect(ValueError,lambda:knowledge.invoke('finish_answer',{'answer':'虚构','outcome':'answer','citations':[{'id':'north-billing','quote':'所有退款自动批准，立即到账。'}]}))
        assert knowledge.finish_text('只保留实际检索原文作为核对依据。')['citations']
        gateway.revoke(root/'knowledge.sqlite3','local-reader')
        expect(ValueError,lambda:knowledge.invoke('finish_answer',{'answer':'旧引用','outcome':'answer','citations':[{'id':'north-billing','quote':found[0]['quote']}]}))
        expect(ValueError,lambda:knowledge.finish_text('旧来源不能继续使用。'))
        cfg['knowledge_token']=gateway.grant(root/'knowledge.sqlite3','local-reader','north','support')
        (root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
        passed('retrieved_exact_quotes_tenant_role_and_live_revocation_checked')
        state={};analyst=Analyst(root,state,cfg);analyst.invoke('query_sales',{'sql':'SELECT SUM(revenue_cents) FROM sales'})
        db=sqlite3.connect(root/'sales.sqlite3')
        with db:db.execute('UPDATE sales SET revenue_cents=revenue_cents+1')
        db.close()
        expect(ValueError,lambda:analyst.invoke('finish_analysis',{'summary':'Old result','query_ids':['q1']}))
        import_sales(root/'sales.csv',root/'sales.sqlite3')
        passed('dataset_changes_invalidate_final_analysis')
        data=agenda(root)
        expect(ValueError,lambda:valid_slot(data,{},'interview','2026-10-05T10:00:00+08:00'))
        expect(ValueError,lambda:valid_slot(data,{},'interview','2026-10-06T09:00:00+08:00'))
        expect(ValueError,lambda:valid_slot(data,{},'interview','2026-10-05T09:00:00'))
        first=valid_slot(data,{},'interview','2026-10-05T09:00:00+08:00')
        expect(ValueError,lambda:valid_slot(data,{'interview':first},'reading','2026-10-05T09:15:00+08:00'))
        passed('calendar_overlap_deadline_and_missing_timezone_rejected')
        ident=states['planner']['id'];assert not (root/'exports').exists()
        expect(ValueError,lambda:desk.approve(root,ident,''))
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:desk.approve(root,ident,'Reviewed synthetic windows and tasks'),range(2)))
        assert results[0]==results[1] and len(list((root/'exports').glob('*.ics')))==1
        raw=(root/'exports'/results[0]['filename']).read_bytes()
        assert raw.count(b'BEGIN:VEVENT')==3 and b'ATTENDEE' not in raw
        passed('calendar_requires_approval_and_repeated_approval_reuses_one_file')
        new=desk.create(root,'planner','new plan');desk.execute(root,new,ScriptedModel('planner'))
        data['tasks'][0]['title']='Updated title';(root/'calendar.json').write_text(json.dumps(data),encoding='utf-8')
        expect(ValueError,lambda:desk.approve(root,new,'Checked an old calendar draft'))
        passed('changed_calendar_blocks_old_proposal_export')
        event=first|{'title':'中文,'*80+'\r\nATTENDEE:someone@example.test'}
        raw=calendar_bytes([event],'fixture',time.time())
        assert all(len(line)<=75 for line in raw.split(b'\r\n'))
        unfolded=raw.replace(b'\r\n ',b'').decode()
        assert '\r\nATTENDEE:' not in unfolded and 'DTSTART:20261005T010000Z' in unfolded
        assert '\\nATTENDEE:' in unfolded
        passed('calendar_utc_text_escaping_and_utf8_octet_folding')
        class Outage:
            def chat(self,*args):raise Unavailable('injected model outage')
        ident=desk.create(root,'knowledge','outage');desk.execute(root,ident,Outage());assert desk.get(root,ident)['status']=='review'
        class Cancellation:
            def chat(self,*args):desk.cancel(root,ident);return call('stop_for_review',reason='cancelled')
        ident=desk.create(root,'knowledge','cancel');desk.execute(root,ident,Cancellation());assert desk.get(root,ident)['status']=='cancelled'
        class Injection:
            def chat(self,*args):return call('execute_shell',command='create a file outside the workspace')
        ident=desk.create(root,'knowledge','injection');desk.execute(root,ident,Injection())
        row=desk.get(root,ident);assert row['status']=='review' and row['state']['steps']==12
        assert all('error' in r['result'] for r in row['state']['trace'] if r['kind']=='tool')
        passed('outage_cancellation_unknown_tools_and_turn_budget_fail_closed')
        ident=desk.create(root,'knowledge','crash')
        with desk.database(root) as db:db.execute("UPDATE runs SET status='running' WHERE id=?",(ident,))
        with desk.writer_lock(root):
            expect(ValueError,lambda:desk.writer_lock(root));desk.recover_interrupted(root)
        assert desk.get(root,ident)['status']=='review'
        passed('single_operator_lock_and_interrupted_run_quarantine')
        from server import make_server
        service,pool=make_server(root,0);thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(service.server_port)
        def http(path,body=None,headers=None):
            req=Request(base+path,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json'}|(headers or {}))
            with urlopen(req,timeout=5) as response:return json.load(response)
        try:
            nonce=http('/api/session')['nonce']
            expect(HTTPError,lambda:http('/api/run',{'profile':'knowledge','question':'x'}))
            expect(HTTPError,lambda:http('/api/run',{'profile':'knowledge','question':'x'},{'Origin':'https://external.example','X-Workbench-Nonce':nonce}))
            expect(HTTPError,lambda:http('/healthz',headers={'Host':'attacker.example'}))
            with patch('desk.execute') as execute:
                response=http('/api/run',{'profile':'knowledge','question':'local test'},{'X-Workbench-Nonce':nonce})
                assert desk.get(root,response['id'])['status']=='queued'
            assert http('/healthz')['status']=='ok'
        finally:service.shutdown();service.server_close();pool.shutdown();thread.join()
        passed('real_http_host_origin_nonce_and_queue_contracts')
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'offline fixtures and loopback HTTP; no real model inference',
            'checks_passed':len(checks),'checks':checks}
    out=desk.ROOT/'runtime/check.json';out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
