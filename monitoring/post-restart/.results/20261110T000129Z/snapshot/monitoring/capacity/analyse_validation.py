#!/usr/bin/env python3
"""Offline derived report for validate.py; source attempts and failures are retained."""
import argparse
from collections import defaultdict
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics

APPS={'java':{'inventory-service','notification-service','order-service','product-service'},'wasm':{'wasmcloud-host'}}

def read(path):return json.loads(Path(path).read_text())

def throttle(telemetry):
    def samples(key):
        result={}
        for row in telemetry.get(key,{}).get('samples',[]):
            match=re.fullmatch(r'([^ ]+\{.*\})\s+(\S+)(?:\s+\S+)?',row['line'])
            if not match:continue
            metric,value=match.groups()
            pod=re.search(r'pod="([^"]+)"',metric)
            if telemetry.get('current_pods') and (not pod or pod.group(1) not in telemetry['current_pods']):continue
            result[(row['node'],metric)]=float(value)
        return result
    before,after=samples('before'),samples('after')
    grouped=defaultdict(lambda:defaultdict(float))
    for key,value in after.items():
        if key not in before or value<before[key]:continue
        metric=key[1];match=re.search(r'container="([^"]+)"',metric)
        if not match:continue
        grouped[match.group(1)][metric.split('{')[0]]+=value-before[key]
    return {container:dict(deltas,throttled_period_fraction=(deltas.get('container_cpu_cfs_throttled_periods_total',0)/deltas['container_cpu_cfs_periods_total'] if deltas.get('container_cpu_cfs_periods_total',0)>0 else None)) for container,deltas in grouped.items()}

def resources(telemetry,implementation):
    output={}
    start,end=telemetry.get('load_start'),telemetry.get('load_end')
    if not start or not end:return dict(error=telemetry.get('error','Missing stage boundaries'))
    for kind in ['cpu','memory']:
        query=telemetry.get('queries',{}).get(kind,{})
        if query.get('status')!='success':
            output[kind]=dict(error='Missing successful telemetry query');continue
        data=defaultdict(dict)
        for series in query['data']['result']:
            if series['metric'].get('container') not in APPS[implementation]:continue
            for stamp,value in series['values']:
                value=float(value)
                if math.isfinite(value):data[stamp][series['metric']['container']]=value
        # Require every application container; never turn missing data into zero.
        full={t:sum(containers.values()) for t,containers in data.items() if set(containers)==APPS[implementation]}
        values=list(full.values())
        output[kind]=dict(samples=len(values),expected_samples=math.floor((end-start)/15)+1,
                          coverage=len(values)/(math.floor((end-start)/15)+1),
                          mean=statistics.mean(values) if values else None,
                          median=statistics.median(values) if values else None,
                          maximum=max(values) if values else None,
                          first_five_minutes_mean=statistics.mean(v for t,v in full.items() if t<start+300) if any(t<start+300 for t in full) else None,
                          last_five_minutes_mean=statistics.mean(v for t,v in full.items() if t>=end-300) if any(t>=end-300 for t in full) else None,
                          points=sorted(full.items()))
    output['throttling']=throttle(telemetry)
    if telemetry.get('error'):output['error']=telemetry['error']
    return output

def derive(directory):
    directory=Path(directory).resolve();journal=read(directory/'validation.json')
    result=dict(kind='derived_summary',state=journal['state'],source_journal=str(directory/'validation.json'),
                protocol=read(directory/'protocol.json'),selected_java_budget=journal.get('selected_java_budget'),jobs=[],repeats={})
    jobs=list(journal['jobs'])
    addendum_path=directory/'stability-addendum.json'
    if addendum_path.exists():
        result['stability_addendum']=read(addendum_path)
        jobs.extend(result['stability_addendum']['jobs'])
        if result['state']=='completed' and result['stability_addendum']['state']!='completed':result['state']='awaiting-stability-followup'
    for job in jobs:
        if not job.get('report'):continue
        path=Path(job['report']);report=read(path)
        row=dict(name=job['name'],source=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                 verified=all(report.get(k) for k in ['completed','restored','applications_healthy']),implementations={})
        for impl,data in report['implementations'].items():
            stages=[]
            for stage in data['stages']+([data['confirmation']] if data.get('confirmation') else []):
                label=impl+'-'+('confirmation' if stage.get('confirmation') else str(stage['rate']))
                source=path.parent/label/'telemetry.json'
                observed=resources(read(source),impl) if source.exists() else dict(error='Missing telemetry')
                supplemental=source.with_name('stability-trends.json')
                extra=None
                if source.exists() and supplemental.exists():
                    extra=read(supplemental)
                    if extra['original_sha256']!=hashlib.sha256(source.read_bytes()).hexdigest():raise ValueError('Supplemental source telemetry hash mismatch')
                    if extra['source_report_sha256']!=hashlib.sha256(path.read_bytes()).hexdigest():raise ValueError('Supplemental source report hash mismatch')
                    smoother=read(source);smoother['queries']['cpu']=extra['queries']['cpu90']
                    improved=resources(smoother,impl)['cpu']
                    if improved.get('coverage',0)>=observed.get('cpu',{}).get('coverage',0):
                        observed['cpu']=improved;observed['cpu']['rate_window_seconds']=90
                stages.append(dict(result=stage,resources=observed,supplemental=extra))
            row['implementations'][impl]=dict(stages=stages,warmup=data.get('warmup'))
            if job['name'].startswith('pair-'):
                result['repeats'].setdefault(impl,[]).append(dict(pair=int(job['name'].split('-')[1]),confirmation=data.get('confirmation'),verified=row['verified']))
        result['jobs'].append(row)
    return result

def sparkline(points,color):
    if len(points)<2:return '<p>Insufficient telemetry</p>'
    x0,x1=points[0][0],points[-1][0];lo=min(v for _,v in points);hi=max(v for _,v in points)
    coordinates=' '.join(f'{60+(t-x0)/(x1-x0)*700:.1f},{115-(v-lo)/(hi-lo or 1)*100:.1f}' for t,v in points)
    return f'<svg viewBox="0 0 780 150" role="img" aria-label="Application resource trend"><g fill="#c4cfdf" font-size="11"><text x="0" y="19">{hi:.2f}</text><text x="0" y="119">{lo:.2f}</text><text x="60" y="142">0 min</text><text x="710" y="142">{(x1-x0)/60:.0f} min</text></g><polyline fill="none" stroke="{color}" stroke-width="2" points="{coordinates}"/></svg>'

def render(directory,result):
    rows=[];charts=[];long_rows=[]
    for job in result['jobs']:
        for impl,data in job['implementations'].items():
            for stage in data['stages']:
                row=stage['result'];observed=stage['resources']
                values=[job['name']+(' confirmation' if row.get('confirmation') else ''),impl,row['rate'],row['accepted'],round(row['p95_ms'],2) if row.get('p95_ms') is not None else '—',row['failures'],row['dropped'],'PASS' if row['passed'] else row['status']]
                rows.append('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in values)+'</tr>')
                if job['name'].startswith('stability-'):
                    cpu=observed.get('cpu',{});memory=observed.get('memory',{})
                    def measured(metrics,key,factor=1):
                        value=metrics.get(key)
                        return 'unavailable' if value is None or metrics.get('coverage',0)<.8 else f'{value/factor:.2f}'
                    cells=[impl,row['rate'],row['accepted'],row['failures'],row['dropped'],round(row['p95_ms'],2),round(row.get('max_ms',0),2),measured(cpu,'mean'),measured(memory,'mean',1024**2),measured(memory,'first_five_minutes_mean',1024**2)+' → '+measured(memory,'last_five_minutes_mean',1024**2),'PASS' if row['passed'] else row['status']]
                    long_rows.append('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in cells)+'</tr>')
                    charts.append('<h2>'+html.escape(job['name'])+'</h2>')
                    for kind,units in [('cpu','cores'),('memory','MiB')]:
                        metrics=observed.get(kind,{})
                        factor=1024**2 if kind=='memory' else 1
                        points=[(t,v/factor) for t,v in metrics.get('points',[])]
                        if not points:charts.append('<p>Missing '+kind+' telemetry</p>');continue
                        first=('unavailable' if metrics['first_five_minutes_mean'] is None else f"{metrics['first_five_minutes_mean']/factor:.2f}");last=('unavailable' if metrics['last_five_minutes_mean'] is None else f"{metrics['last_five_minutes_mean']/factor:.2f}")
                        charts.append(f'<h3>{kind}: first / last 5-minute means {first} / {last} {units}</h3><p>Coverage {metrics["coverage"]:.0%}. Each chart has its own vertical scale.</p>'+sparkline(points,'#83d4fc' if impl=='java' else '#b5f5a4'))
                    extra=stage.get('supplemental')
                    if extra:
                        series=extra['queries']['latency_p95_cumulative']['data']['result']
                        if len(series)==1:
                            points=[(t,float(value)*1000) for t,value in series[0]['values'] if math.isfinite(float(value))]
                            charts.append('<h3>Cumulative request p95 during this run, ms</h3><p>This is accumulated since this stage began, not a rolling-window percentile.</p>'+sparkline(points,'#ffca85'))
    cards=[]
    for impl,repeats in result['repeats'].items():
        confirmations=[r['confirmation'] for r in repeats if r['verified'] and r.get('confirmation') and r['confirmation']['passed']]
        text=', '.join(str(r['rate']) for r in confirmations) or 'none'
        cards.append('<article><h2>'+impl+'</h2><p>Passing highest-candidate 5-minute repeat rates: <b>'+text+'</b> orders/s</p><p>'+str(len(confirmations))+' / '+str(len(repeats))+' highest candidates confirmed. Lower short steps and long follow-ups are retained below.</p></article>')
    document='''<!doctype html><meta charset="utf-8"><title>Throughput controls and stability</title><style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1200px;margin:40px auto}section{display:flex;gap:20px}article{background:#21334b;padding:20px;flex:1;border-radius:10px}td,th{padding:10px;border-bottom:1px solid #456;text-align:left}svg{width:100%;max-height:200px;background:#1d293e}a{color:#8df}</style><h1>Throughput controls and stability</h1>'''
    document+='<p>State: '+result['state']+'. Selected Java CPU allocation: '+str(result.get('selected_java_budget'))+'.</p><section>'+''.join(cards)+'</section>'
    if long_rows:
        document+='<h2>Matched 30-minute trials</h2><p>CPU values are mean smoothed rates in cores. RAM is application working-set memory in MiB; first/last values are five-minute means. Metrics with less than 80% complete-container coverage are withheld.</p><table><tr>'+''.join('<th>'+label+'</th>' for label in ['App','Offered/s','Accepted','HTTP/body errors','Dropped arrivals','p95 ms','Max ms','Mean CPU','Mean RAM MiB','RAM first → last','Gate'])+'</tr>'+''.join(long_rows)+'</table><p>Dropped arrivals were never issued; they are separate from failed HTTP requests. A failed zero-drop gate does not establish an intrinsic application throughput ceiling. Memory growth in one trial is not proof of a leak.</p>'
    document+='<p>Equal 2-core / 4-GiB application budgets. Java divides CPU between services; Wasm pools CPU in one host. Three alternating-order pairs are evidence about this setup, not population confidence intervals. Rates differ: compare p95 as rate-specific observations. Shared infrastructure excluded.</p>'
    document+='<p>Source reports: '+ ' · '.join('<a href="'+html.escape(str(Path(job['source']).with_name('report.html')))+'">'+html.escape(job['name'])+'</a>' for job in result['jobs'])+'</p>'
    document+='<table><tr>'+''.join('<th>'+s+'</th>' for s in ['Job','App','Rate/s','Accepted','p95 ms','Errors','Dropped','Gate'])+'</tr>'+''.join(rows)+'</table>'+''.join(charts)
    document+='<p>CPU/RAM trends use only current application containers, with complete-container samples required. Wasm sidecar is excluded. CFS counters bracket whole stages including idle/recovery. Missing telemetry is never zero-filled. A 30-minute pass does not rule out longer-term growth or short-interval latency spikes. All probes, failures and source hashes are retained in analysis.json and validation.json.</p>'
    (Path(directory)/'analysis.html').write_text(document)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);args=parser.parse_args()
    result=derive(args.directory);(args.directory/'analysis.json').write_text(json.dumps(result,indent=2)+'\n');render(args.directory,result)
    print(args.directory/'analysis.html')
