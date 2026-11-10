#!/usr/bin/env python3
"""Summarise the captured SQL/protocol, wait and process-CPU observations."""
from collections import Counter, defaultdict
import argparse
import hashlib
import html
import json
from pathlib import Path

def read(path):return json.loads(path.read_text())
def metrics(value):return {x['statistic']:x['value'] for x in value.get('measurements',[])}
def main(directory):
    raw=read(directory/'report.json')
    if raw['state']!='completed' or not raw.get('logging_restored'):raise RuntimeError('Profile incomplete or PostgreSQL logging not verified restored')
    derived={'method':raw['method'],'limitations':raw.get('limitations',[])+[
      'These are diagnostic runs with statement logging; different log volumes can affect overhead. Do not treat their HTTP timings as an uninstrumented runtime benchmark.',
      'Statement-duration observations are server elapsed timings, not server CPU timings or complete provider-call durations.',
      'Wasm still uses implicit PostgreSQL transactions. No explicit BEGIN/COMMIT messages does not mean zero transaction or commit cost. Logged extended-protocol phases are not guaranteed to account for implicit transaction completion; do not compare their sums as complete database-path latency.',
      'PostgreSQL wait events are sampled. Idle ClientRead is not pool contention; a missed short wait is not proof of no waiting.',
      'Wasm deadpool acquisition latency is not exported by the stock provider. Backend samples cannot measure requests queued before obtaining a database connection.',
      'Native process CPU includes host and its direct provider processes, not NATS sidecars/brokers or PostgreSQL server processes. This is a process-cost profile, not a function-level CPU flamegraph.'
    ],'implementations':{}}
    for impl,stage in raw['stages'].items():
        count=stage['k6']['metrics']['http_reqs']['count'];dbs=['order-service','inventory-service'] if impl=='java' else ['platform-poc-wasm']
        selected=[x for x in stage['sql'] if x['database'] in dbs]
        actual=[x for x in selected if x['phase'] in ['execute','statement'] and x['sql']]
        data=[x for x in actual if x['sql'].lower().startswith(('select ','insert ','with ','update ','delete '))]
        control=[x for x in actual if x['sql'].lower().startswith(('begin','commit','rollback'))]
        phases={phase:dict(events=sum(x['timing_ms']['count'] for x in selected if x['phase']==phase),server_elapsed_ms=sum(x['timing_ms']['total'] for x in selected if x['phase']==phase)) for phase in ['parse','bind','execute','statement']}
        pools=read(directory/f'{impl}-pools.json');poolstats={}
        for svc in ['order-service','inventory-service']:
            a=metrics(pools['before'][svc]['hikaricp.connections.acquire']);b=metrics(pools['after'][svc]['hikaricp.connections.acquire'])
            pending=[metrics(x['metrics'][svc]['hikaricp.connections.pending']).get('VALUE') for x in pools['samples']]
            active=[metrics(x['metrics'][svc]['hikaricp.connections.active']).get('VALUE') for x in pools['samples']]
            da=b.get('COUNT',0)-a.get('COUNT',0)
            poolstats[svc]=dict(acquisitions=da if a and b else None,mean_acquire_ms=(b['TOTAL_TIME']-a['TOTAL_TIME'])*1000/da if da>0 else None,
                pending_sample_max=max((x for x in pending if x is not None),default=None),active_sample_max=max((x for x in active if x is not None),default=None))
        activity=read(directory/f'{impl}-activity.json');waits=Counter();states=Counter();sessions=defaultdict(set);maxconnections=Counter();active_count=0
        for sample in activity:
            perdb=Counter()
            for pid,db,user,app,state,kind,event,age in sample['rows']:
                if db not in dbs:continue
                sessions[db].add(pid);perdb[db]+=1;states[state]+=1
                if state=='active':waits[(kind or 'none',event or 'none')]+=1;active_count+=1
            for db,n in perdb.items():maxconnections[db]=max(n,maxconnections[db])
        item=dict(requests=count,http_duration_ms=stage['k6']['metrics']['http_req_duration'],failed_checks=stage['k6']['metrics']['checks']['fails'],
             data_statements_per_order=sum(x['timing_ms']['count'] for x in data)/count,
             transaction_commands_per_order=sum(x['timing_ms']['count'] for x in control)/count,
             data_execution_ms_per_order=sum(x['timing_ms']['total'] for x in data)/count,
             transaction_execution_ms_per_order=sum(x['timing_ms']['total'] for x in control)/count,
             phases=phases,sql=data+control,pools=poolstats,
             activity=dict(samples=len(activity),states=dict(states),active_connection_observations=active_count,
                           active_wait_events=[dict(type=k[0],event=k[1],observations=v) for k,v in waits.most_common()],
                           distinct_backend_pids={k:len(v) for k,v in sessions.items()},max_sampled_connections=dict(maxconnections)),
             native_processes=stage['native_processes'])
        derived['implementations'][impl]=item
    paths=[directory/'report.json']+list(directory.glob('*-activity.json'))+list(directory.glob('*-pools.json'))+list(directory.glob('*-statements.json'))
    derived['sources']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    (directory/'analysis.json').write_text(json.dumps(derived,indent=2)+'\n')
    esc=lambda x:html.escape(str(x))
    page='<!doctype html><meta charset="utf-8"><title>PostgreSQL provider profile</title><style>body{font:17px system-ui;max-width:1280px;margin:40px auto;padding:20px;color:#eaf1fa;background:#101c2b}table{border-collapse:collapse;width:100%;margin:20px 0}th,td{text-align:left;padding:10px;border-bottom:1px solid #456}code{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}p,li{line-height:1.5}a{color:#69aeff}h1,h2{color:#55dfc1}</style><h1>PostgreSQL provider profile</h1><p>100 orders/s · 60s warmup then90s measured per platform · unchanged application images. PostgreSQL logging settings restored. This is a separate diagnostic study.</p><table><tr><th>Metric</th><th>Java</th><th>Wasm</th></tr>'
    for label,key,fmt in [('Requests','requests','{:,.0f}'),('Failed checks','failed_checks','{:,.0f}'),('Data statements/order','data_statements_per_order','{:.3f}'),('Explicit transaction commands/order','transaction_commands_per_order','{:.3f}'),('Logged data SQL execution/order','data_execution_ms_per_order','{:.3f} ms'),('Logged explicit transaction execution/order','transaction_execution_ms_per_order','{:.3f} ms')]:
        page+='<tr><td>'+label+'</td>'+''.join('<td>'+fmt.format(derived['implementations'][i][key])+'</td>' for i in ['java','wasm'])+'</tr>'
    page+='</table>'
    for impl,item in derived['implementations'].items():
        page+='<h2>'+impl.title()+'</h2><h3>Executed SQL</h3><table><tr><th>Database</th><th>SQL</th><th>Calls</th><th>Mean ms</th><th>p95 ms</th></tr>'
        for row in item['sql']:
            t=row['timing_ms'];page+='<tr><td>'+esc(row['database'])+'</td><td><code>'+esc(row['sql'])+'</code></td><td>'+str(t['count'])+'</td><td>'+f'{t["mean"]:.4f}'+'</td><td>'+f'{t["p95"]:.4f}'+'</td></tr>'
        page+='</table><h3>Protocol phases and waits</h3><pre>'+esc(json.dumps({k:item[k] for k in ['phases','pools','activity']},indent=2))+'</pre><h3>Native Wasm process CPU during this stage</h3><table><tr><th>Process</th><th>CPU seconds</th><th>Mean cores</th></tr>'
        for proc in item['native_processes']:page+='<tr><td>'+esc(proc['executable'].split('/')[-1])+'</td><td>'+f'{proc["cpu_seconds"]:.3f}'+'</td><td>'+f'{proc["mean_cores"]:.4f}'+'</td></tr>'
        page+='</table>'
    page+='<h2>Interpretation limits</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in derived['limitations'])+'</ul><p><a href="analysis.json">Derived metrics and source hashes</a> · <a href="report.json">Raw execution journal</a></p>'
    (directory/'analysis.html').write_text(page)
    print(directory/'analysis.html')
    print(json.dumps({i:{k:v for k,v in x.items() if k not in ['sql','native_processes','http_duration_ms']} for i,x in derived['implementations'].items()},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    main(parser.parse_args().directory)
