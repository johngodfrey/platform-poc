#!/usr/bin/env python3
"""Derived analysis; does not modify frozen inputs or raw benchmark evidence."""
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics

ROOT=None


def read(path):return json.loads(Path(path).read_text())


def report_paths(attempt):
    paths=list(attempt.get('reports',[]))
    log=Path(attempt['directory'])/'driver.log'
    if not paths and log.exists():
        pattern=r'Resource backup: (.+)/state.json' if attempt['suite']=='density' else r'Report: (.+)/report.html'
        paths=list(dict.fromkeys(re.findall(pattern,log.read_text())))
    return paths


def stats(values):
    return dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else dict(n=0)


def shared_cpu(cpu):
    samples={}
    expected=set(cpu['expected_nodes'])
    for series in cpu.get('samples',[]):
        node=series['metric']['node']
        for timestamp,value in series['values']:
            value=float(value)
            if math.isfinite(value):samples.setdefault(float(timestamp),{})[node]=value
    complete=[sum(x.values()) for x in samples.values() if set(x)==expected]
    coverage=len(complete)/max(1,max(cpu.get('coverage',{}).values(),default=0))
    valid=bool(cpu.get('complete')) and coverage>=.8 and bool(complete)
    return dict(complete=valid,coverage=coverage,max_fraction=max(complete) if complete else None,
                headroom=valid and max(complete)<.6)


def density(attempt,impl):
    stages=[]
    for directory in report_paths(attempt):
        directory=Path(directory)
        if not (directory/'report.json').exists():continue
        report=read(directory/'report.json')
        for stage in report.get('implementations',{}).get(impl,[]):
            row=dict(stage);row['driver_status']=stage['status'];row['global_cpu_checks']=[]
            evidence=directory/(impl+'-'+str(stage['memory_mib'])+'-pod-evidence.json')
            row['observed_oom_containers']=[]
            if evidence.exists():
                for pods in read(evidence).values():
                    if not isinstance(pods,list):continue
                    for pod in pods:
                        limits={c['name']:c.get('resources',{}).get('limits',{}).get('memory') for c in pod['spec']['containers']}
                        expected=str(stage['memory_mib']//(4 if impl=='java' else 1))+'Mi'
                        for c in pod.get('status',{}).get('containerStatuses',[]):
                            if limits.get(c['name'])==expected and any(c.get(k,{}).get('terminated',{}).get('reason')=='OOMKilled' for k in ('state','lastState')):
                                row['observed_oom_containers'].append(c['name'])
                row['observed_oom_containers']=sorted(set(row['observed_oom_containers']))
            for file in sorted((directory/(impl+'-'+str(stage['memory_mib']))).glob('*/result.json')):
                result=read(file)
                check=shared_cpu(result['cpu'])
                row['global_cpu_checks'].append(dict(stage=file.parent.name,**check))
            bad_cpu=not row['global_cpu_checks'] or not all(c['headroom'] for c in row['global_cpu_checks'])
            if row['status']=='pass' and len(row['global_cpu_checks'])!=3:bad_cpu=True
            if row['status'] in ('pass','fail') and bad_cpu:
                row['status']='inconclusive_shared_vm_cpu'
            row['reports']=str(directory);stages.append(row)
    passing=[s['memory_mib'] for s in stages if s['status']=='pass']
    minimum=min(passing) if passing else None
    return dict(sweep=attempt['pair'],lowest_passing_mib=minimum,projected_slots_8192_mib=8192//minimum if minimum else None,stages=stages)


def analyse():
    campaign=read(ROOT/'campaign.json')
    attempts=campaign['attempts']
    result=dict(status=campaign['status'],protocol=campaign['protocol'],implementations={},paired_footprint_ratios={},attempt_outcomes=dict(Counter(a['status'] for a in attempts)))
    for impl in ('java','wasm'):
        rows=[a for a in attempts if a['suite']=='footprint' and a.get('implementation')==impl]
        valid=[a for a in rows if a.get('measured') and a['status']=='pass']
        fp=dict(scheduled=5,attempted=len(rows),usable=len(valid),outcomes=dict(Counter(a['status'] for a in rows)),runs=rows)
        for phase in ('idle','load'):
            fp[phase]={metric:stats([a['telemetry'][phase][metric]['median']/(1048576 if metric=='memory_bytes' else 1) for a in valid]) for metric in ('cpu_cores','memory_bytes')}
        fp['p95_ms']=stats([a['result']['p95_ms'] for a in valid])
        output=dict(footprint=fp,density=[density(a,impl) for a in attempts if a['suite']=='density' and a.get('implementation')==impl])
        startup=[]
        for a in attempts:
            if a['suite']=='startup_cache':
                for directory in report_paths(a):
                    path=Path(directory)/'report.json'
                    if path.exists():
                        startup.extend(dict(r,source=directory) for r in read(path).get('trials',[]) if r['implementation']==impl)
        pairs={r['pair'] for r in startup}
        valid_pairs={pair for pair in pairs if len([r for r in startup if r['pair']==pair])==2 and {r['cache_mode'] for r in startup if r['pair']==pair}=={'cold','cached'} and all(r.get('valid') for r in startup if r['pair']==pair)}
        output['startup_cache']=dict(scheduled_pairs=5,usable_pairs=len(valid_pairs),missing_or_invalid_pairs=5-len(valid_pairs),all_trials=startup)
        for mode in ('cold','cached'):
            output['startup_cache'][mode]=stats([r['first_order_seconds'] for r in startup if r['pair'] in valid_pairs and r['cache_mode']==mode])
        result['implementations'][impl]=output
    for phase in ('idle','load'):
        for metric in ('cpu_cores','memory_bytes'):
            ratios=[]
            for pair in range(1,6):
                rows={a['implementation']:a for a in attempts if a['suite']=='footprint' and a['pair']==pair and a['status']=='pass'}
                if set(rows)=={'java','wasm'}:
                    java=rows['java']['telemetry'][phase][metric]['median']
                    if java>0:ratios.append(rows['wasm']['telemetry'][phase][metric]['median']/java)
            result['paired_footprint_ratios'][phase+'_'+metric+'_wasm_over_java']=stats(ratios)
    result['notes']=[
        'Each footprint point is a per-run median of complete samples inside a fixed phase. memory_bytes summaries above are expressed in MiB.',
        'Only complete, passing footprint pairs enter paired ratios; every other attempt remains listed.',
        'Density applies both the original per-node gate and the conservative shared-VM interpretation registered before density began.',
        'Legacy density memory sums include terminated-pod samples around rollouts. Supplemental fresh per-pod reconstruction is archived separately; allocation/latency/reliability/CPU gates never use that legacy memory sum.',
        'A smallest passing allocation is a tested bound, not a proven minimum. Readiness failure is not proof of OOM. Slots are projections.',
        'A startup pair contributes only if both cold and cached members are valid. All raw startups remain listed.',
        'Footprint uses existing warm processes. Density restarts processes under memory caps. Startup creates fresh workers with warm dependencies.',
        'Historical derived reports are not independent trials. No historical trials are pooled into this new frozen campaign.',
    ]
    return result


def fmt(value,digits=2):
    return '—' if not value.get('n') else f'{value["median"]:.{digits}f} [{value["minimum"]:.{digits}f}–{value["maximum"]:.{digits}f}], n={value["n"]}'


def page(data):
    esc=lambda x:html.escape(str(x))
    out='<!doctype html><meta charset="utf-8"><title>Validated Java and Wasm comparisons</title><style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1200px;margin:35px auto;padding:20px}td,th{padding:12px;border-bottom:1px solid #456;text-align:left}table{border-collapse:collapse;width:100%}a{color:#8bd}.bar{height:22px;display:inline-block;vertical-align:middle;margin-right:10px}small{color:#bbc}</style><h1>Java and Wasm: frozen validation</h1><p>Status: '+esc(data['status'])+' · <a href="report.html">Campaign journal</a> · <a href="history.html">Historical evidence</a> · <a href="history-review.json">Historical interpretation</a> · <a href="freeze.json">Frozen inputs</a></p><p>Values are medians [observed min–max] across separate runs. n counts separate usable runs, not requests or statistically independent process instances. All failed or incomplete attempts remain in the journal.</p>'
    out+='<h2>Warm application footprint</h2><table><tr><th>Measure</th><th>Java</th><th>Wasm</th></tr>'
    for phase in ('idle','load'):
        for metric,label in [('memory_bytes','memory, MiB'),('cpu_cores','CPU cores')]:
            out+='<tr><td>'+phase+' '+label+'</td>'+''.join('<td>'+fmt(data['implementations'][impl]['footprint'][phase][metric],3 if metric=='cpu_cores' else 1)+'</td>' for impl in ('java','wasm'))+'</tr>'
    out+='<tr><td>Order p95, ms</td>'+''.join('<td>'+fmt(data['implementations'][impl]['footprint']['p95_ms'])+'</td>' for impl in ('java','wasm'))+'</tr></table><h3>Loaded memory</h3>'
    maximum=max([data['implementations'][impl]['footprint']['load']['memory_bytes'].get('median',0) for impl in ('java','wasm')]+[1])
    for impl,color in [('java','#5794f2'),('wasm','#73bf69')]:
        v=data['implementations'][impl]['footprint']['load']['memory_bytes']
        out+='<p>'+impl+' <span class="bar" style="background:'+color+';width:'+str(65*v.get('median',0)/maximum)+'%"></span>'+fmt(v,1)+' MiB</p>'
    out+='<h3>Every footprint attempt</h3><p>Warm processes are reused across repetitions. Run order is retained so memory drift cannot be hidden by an aggregate.</p><table><tr><th>Pair</th><th>Implementation</th><th>Outcome</th><th>Loaded CPU</th><th>Loaded MiB</th><th>p95 ms</th><th>Accepted / failed / dropped</th></tr>'
    runs=read(ROOT/'campaign.json')['attempts']
    for a in runs:
        if a['suite']!='footprint':continue
        cpu=a.get('telemetry',{}).get('load',{}).get('cpu_cores',{}).get('median')
        memory=a.get('telemetry',{}).get('load',{}).get('memory_bytes',{}).get('median')
        r=a.get('result',{})
        vals=(a['pair'],a['implementation'],a['status'],'—' if cpu is None else round(cpu,3),'—' if memory is None else round(memory/1048576,1),round(r['p95_ms'],2) if 'p95_ms' in r else '—',' / '.join(str(r.get(k,'—')) for k in ('accepted','failures','dropped')))
        out+='<tr>'+''.join('<td>'+esc(v)+'</td>' for v in vals)+'</tr>'
    out+='</table>'
    out+='<h2>Memory density with CPU headroom</h2><p>25 orders/s, p95 &lt;100ms, no failures/drops, no CPU limits, two measurement repeats per cap. Both per-node and combined cluster CPU gates apply. Slots exclude infrastructure reserves and are not a deployed replica count.</p><table><tr><th>Implementation</th><th>Sweep</th><th>Smallest passing MiB</th><th>Projected slots / 8 GiB</th><th>All tested outcomes</th></tr>'
    for impl in ('java','wasm'):
        for sweep in data['implementations'][impl]['density']:
            outcomes=', '.join(str(s['memory_mib'])+': '+s['status']+(' (OOMKilled observed: '+', '.join(s['observed_oom_containers'])+')' if s.get('observed_oom_containers') else '') for s in sweep['stages'])
            out+='<tr>'+''.join('<td>'+esc(v)+'</td>' for v in (impl,sweep['sweep'],sweep['lowest_passing_mib'],sweep['projected_slots_8192_mib'],outcomes))+'</tr>'
    out+='</table><p><a href="memory-reconstructed-summary.json">Fresh per-pod density memory reconstruction</a> (excludes stale terminated pods; original results retained).</p><h2>Container-image cold versus cached startup</h2><p>First accepted real order, seconds. Databases remain running. Both members of an invalid cold/cached pair are excluded together.</p><table><tr><th>Implementation</th><th>Cold</th><th>Cached</th><th>Usable pairs / 5</th></tr>'
    for impl in ('java','wasm'):
        s=data['implementations'][impl]['startup_cache'];out+='<tr><td>'+impl+'</td><td>'+fmt(s['cold'])+'</td><td>'+fmt(s['cached'])+'</td><td>'+str(s['usable_pairs'])+'</td></tr>'
    out+='</table><h2>Limits and provenance</h2><ul>'+''.join('<li>'+esc(s)+'</li>' for s in data['notes'])+'</ul><p><a href="analysis.json">Full derived data, including all attempts</a> · <a href="density-shared-vm-policy.json">Registered shared-VM gate</a> · <a href="registry-artifact-freeze.json">Local artifact digests</a></p>'
    if data['status'] in ('preparing','running'):out=out.replace('<title>','<meta http-equiv="refresh" content="60"><title>',1)
    return out


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign',type=Path,help='Completed campaign directory; raw results are retained')
    parser.add_argument('--refresh-memory',action='store_true',help='Reconstruct missing fresh pod memory from local Prometheus; requires archived pod observations')
    args=parser.parse_args()
    global ROOT
    ROOT=args.campaign.resolve()
    if args.refresh_memory:
        import reconstruct_memory
        reconstruct_memory.ROOT=ROOT
        reconstruct_memory.run()
    data=analyse()
    (ROOT/'analysis.json').write_text(json.dumps(data,indent=2)+'\n')
    (ROOT/'analysis.html').write_text(page(data))
    print(ROOT/'analysis.html')


if __name__=='__main__':main()
