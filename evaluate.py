"""Real local-model runs on fixed synthetic cases; no calendar export or paid APIs."""
from datetime import datetime,timezone
import argparse
import json
from pathlib import Path
import time
import uuid
import desk


CASES=[
    ('knowledge','北区客户申请退款需要提供什么？谁复核、多久复核、客服能直接批准吗？请给出原文引用。'),
    ('knowledge','资料里是否有北区企业差旅报销的酒店标准？没有证据时请停止作答，不要给出数字。'),
    ('analyst','Compare July and August 2026 total revenue and break the change down by region. Use SQL; report CNY, not cents. Do not invent causes.'),
    ('planner','Read the agenda and schedule ALL three tasks in available windows, before deadlines, without overlap. Propose the calendar for human approval.')
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=desk.ROOT/'runtime/model-evaluation.json')
    parser.add_argument('--profile',choices=['knowledge','analyst','planner'])
    args=parser.parse_args()
    root=desk.ROOT/'runtime'/('evaluation-'+uuid.uuid4().hex[:8]);desk.initialize(root)
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'provider':'ollama','model':'qwen3-vl:2b-instruct',
            'scope':'synthetic cases; one run each; no exports or paid API calls',
            'quality_boundary':'completion and machine-checkable evidence only, not independently graded answer quality','cases':[]}
    for profile,question in CASES:
        if args.profile and args.profile!=profile:continue
        ident=desk.create(root,profile,question);start=time.perf_counter();desk.execute(root,ident)
        row=desk.get(root,ident)
        result=row['state'].get('result',{})
        report['cases'].append({'profile':profile,'question':question,'status':row['status'],'error':row['error'],
            'latency_ms':round((time.perf_counter()-start)*1000),'steps':row['state']['steps'],'tokens':row['state']['tokens'],
            'result':result,'trace':row['state']['trace']})
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'profile':profile,'status':row['status'],'steps':row['state']['steps'],'error':row['error']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
