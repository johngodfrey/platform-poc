#!/usr/bin/env python3
"""Read-only comparison summary with optional archived Prometheus memory data."""
from collections import defaultdict
import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import statistics
import urllib.parse
import urllib.request

def stats(values):return dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values))
def main(directory):
    path=directory/'report.json';data=json.loads(path.read_text())
    if data['state']!='completed' or not data['restored'] or not data.get('logging_restored'):raise RuntimeError('Comparison incomplete or restoration unverified')
    groups=defaultdict(list)
    result=dict(source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),restored=True,logging_restored=True,phases=[],groups={},limitations=[
      'Two fresh-host repetitions per same-build variant; one separate stock observation. This is a fixed100/s test, not a capacity or long-term stability test.',
      'Process CPU counters cover the measurement command including its brief idle/recovery margins; CPU per1000 accepted orders normalizes work.',
      'Application CPU is the host plus direct native providers; shared NATS, PostgreSQL and control-plane CPU are excluded.',
      'Changes relative to stock may reflect build differences. The uncached-control versus cached comparison uses the same compiler/flags/dependency lock.',
      'SQL probes run separately with diagnostic logging and are excluded from the CPU and latency measurements.',
      'Memory, if present, is sampled working-set data; it is not a minimum safe allocation or proof of a long-term plateau.'
    ])
    if data.get('predecessor'):
        result['predecessor']=data['predecessor']
        result['limitations'].append('Completed first-pair measurements are reused unchanged from a retained predecessor. A deployment-only duplicate-version error and verified recovery separate the pairs; source/binary/measurement settings were unchanged.')
    for phase in data['phases']:
        row=dict(name=phase['name'],kind=phase['kind'],result=phase['result'],sql_probe=phase.get('sql_probe'),provider_sha256=phase['processes']['provider_sha256'],reused=phase.get('reused',False))
        groups[phase['kind']].append(row)
        source=Path(phase.get('source_directory',directory/phase['name']))
        if phase.get('reused') and hashlib.sha256((source/'measurement/summary.json').read_bytes()).hexdigest()!=phase['source_summary_sha256']:
            raise RuntimeError('Retained measurement summary changed')
        output=directory/phase['name']/'memory.json'
        output.parent.mkdir(exist_ok=True)
        if not output.exists():
            try:
                log=(source/'measurement/k6.log').read_text()
                match=re.search(r'start_ms\\?"\s*:\s*(\d+)',log)
                if not match:raise ValueError('No phase start marker')
                start=int(match[1])/1000+1
                query='container_memory_working_set_bytes{job="kubelet-cadvisor",namespace="wasm",container="wasmcloud-host",pod="'+phase['processes']['pod']+'"}'
                url='http://localhost:8080/prometheus/api/v1/query_range?'+urllib.parse.urlencode(dict(query=query,start=start,end=start+120,step=5))
                with urllib.request.urlopen(url,timeout=20) as response:memory=json.load(response)
                output.write_text(json.dumps(dict(query=query,start=start,end=start+120,data=memory),indent=2)+'\n')
            except Exception as error:output.write_text(json.dumps(dict(error=str(error)))+'\n')
        memory=json.loads(output.read_text());series=memory.get('data',{}).get('data',{}).get('result',[])
        if len(series)==1 and len(series[0]['values'])>=20:
            values=[float(v)/2**20 for _,v in series[0]['values']]
            row['memory_mib']=dict(mean=statistics.mean(values),maximum=max(values),samples=len(values))
        else:row['memory_unavailable']=memory.get('error','Incomplete or ambiguous series')
        result['phases'].append(row)
    for kind,rows in groups.items():
        result['groups'][kind]={key:stats([row['result'][key] for row in rows]) for key in ['application_cores','postgres_provider_cores','cpu_seconds_per_1000_orders','p95_ms','median_ms','mean_ms']}
        result['groups'][kind]['all_loads_clean']=all(not x['result']['failures'] and not x['result']['dropped'] for x in rows)
    a,b=result['groups']['control'],result['groups']['cached']
    result['relative_to_same_build_control']={key:dict(ratio=b[key]['median']/a[key]['median'],reduction_percent=100*(1-b[key]['median']/a[key]['median'])) for key in ['application_cores','postgres_provider_cores','cpu_seconds_per_1000_orders','p95_ms','median_ms','mean_ms']}
    result['all_matched_loads_clean']=a['all_loads_clean'] and b['all_loads_clean']
    result['parse_verification']={row['name']:dict(parse_per_order=row['sql_probe']['parse_per_order'],execute_per_order=row['sql_probe']['execute_per_order']) for row in result['phases'] if row.get('sql_probe')}
    (directory/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    e=lambda v:html.escape(str(v))
    page='<!doctype html><meta charset="utf-8"><title>Prepared-statement cache performance</title><style>body{font:18px system-ui;max-width:1250px;margin:40px auto;padding:20px;background:#101c2b;color:#eef}td,th{padding:12px;text-align:left;border-bottom:1px solid #456}table{border-collapse:collapse;width:100%}a{color:#7bd}li,p{line-height:1.5}</style><h1>Prepared-statement cache performance</h1><p>100 orders/s;60s warmup then120s measured per phase. Same-build uncached/cached variants run twice. Original provider and database logging restored.</p><table><tr><th>Phase</th><th>Accepted</th><th>Failed/dropped</th><th>App cores</th><th>PG cores</th><th>CPU-sec/1000</th><th>p95 ms</th><th>Mean RAM MiB</th></tr>'
    for phase in result['phases']:
        v=phase['result'];cells=[phase['name'],v['accepted'],str(v['failures'])+'/'+str(v['dropped']),f'{v["application_cores"]:.4f}',f'{v["postgres_provider_cores"]:.4f}',f'{v["cpu_seconds_per_1000_orders"]:.3f}',f'{v["p95_ms"]:.3f}',f'{phase["memory_mib"]["mean"]:.1f}' if phase.get('memory_mib') else 'unavailable']
        page+='<tr>'+''.join('<td>'+e(c)+'</td>' for c in cells)+'</tr>'
    page+='</table><h2>Same-build median changes</h2><ul>'
    for key,x in result['relative_to_same_build_control'].items():page+='<li>'+e(key)+': '+f'{x["reduction_percent"]:.1f}%'+' reduction</li>'
    page+='</ul><p>All same-build loads clean: '+e(result['all_matched_loads_clean'])+'</p><h2>Separate SQL verification</h2><pre>'+e(json.dumps(result['parse_verification'],indent=2))+'</pre><h2>Limits</h2><ul>'+''.join('<li>'+e(x)+'</li>' for x in result['limitations'])+'</ul><p><a href="report.html">Raw execution report</a> · <a href="analysis.json">Derived values</a></p>'
    (directory/'analysis.html').write_text(page)
    print(directory/'analysis.html');print(json.dumps(result['relative_to_same_build_control'],indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    main(parser.parse_args().directory)
