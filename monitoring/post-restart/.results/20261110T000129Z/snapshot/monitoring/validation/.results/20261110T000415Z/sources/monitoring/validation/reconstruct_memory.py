"""Supplemental fresh per-pod RAM, leaving original density results untouched."""
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import urllib.parse
import urllib.request

ROOT=None
JAVA=['inventory-service','notification-service','order-service','product-service']


def read(p):return json.loads(Path(p).read_text())


def mib(value):
    match=re.fullmatch(r'([0-9]+)(Mi|Gi)',str(value))
    return int(match[1])*(1024 if match[2]=='Gi' else 1) if match else None


def choose_pods(observations,impl,cap,start,end):
    options=[]
    names=JAVA if impl=='java' else ['wasmcloud-host']
    for observation in observations:
        if not start-20<=observation['time']<=end+20:continue
        pods=observation['namespaces'].get(impl,[])
        if not isinstance(pods,list) or len(pods)!=len(names):continue
        selected=[]
        for pod in pods:
            if not pod['ready'] or pod.get('deleting'):continue
            for c in pod['containers']:
                if c['name'] in names and mib(c.get('resources',{}).get('limits',{}).get('memory'))==cap//len(names):
                    selected.append((pod['name'],c['name']))
        if len(selected)==len(names) and {c for _,c in selected}==set(names):options.append((observation['time'],selected))
    unique={tuple(sorted(pairs)) for _,pairs in options}
    if len(unique)>1:raise RuntimeError('Multiple pod identity sets in one load interval')
    return (list(next(iter(unique))),[t for t,_ in options]) if unique else ([],[])


def run():
    campaign=read(ROOT/'campaign.json')
    observer=ROOT/'pod-observations.jsonl'
    observations=[]
    if observer.exists():
        for line in observer.read_text().splitlines():
            try:observations.append(json.loads(line))
            except ValueError:pass
    summaries=[]
    for attempt in campaign['attempts']:
        if attempt['suite']!='density':continue
        reports=list(attempt.get('reports',[]))
        log=Path(attempt['directory'])/'driver.log'
        if not reports and log.exists():
            matches=re.findall(r'Resource backup: (.+)/state.json',log.read_text())
            reports=list(dict.fromkeys(matches))
        for directory in reports:
            directory=Path(directory);impl=attempt['implementation']
            for result_file in sorted(directory.glob(impl+'-*/*/result.json')):
                relative=result_file.relative_to(directory)
                target=ROOT/'memory-reconstructed'/directory.name/relative.with_name('memory.json')
                if target.exists():summaries.append(read(target));continue
                result=read(result_file);cap=int(relative.parts[0].rsplit('-',1)[1])
                times=[float(t) for series in result['cpu'].get('samples',[]) for t,_ in series['values']]
                if not times:continue
                start,end=min(times),max(times)
                try:
                    expected,observation_times=choose_pods(observations,impl,cap,start,end)
                    provenance='Supplemental live pod/limit observations'
                    pin=ROOT/'density-first-stage-pod-pin.json'
                    if not expected and impl=='java' and cap==4096 and pin.exists() and Path(read(pin)['run'])==directory:
                        expected=[(p,next(n for n in JAVA if p.startswith(n+'-'))) for p in read(pin)['pods']]
                        provenance=read(pin)['source']
                    if not expected:continue
                    selector='job="kubelet-cadvisor",namespace="'+impl+'",pod=~"'+'|'.join(p for p,_ in expected)+'",container=~"'+'|'.join(c for _,c in expected)+'"'
                    metric='container_memory_working_set_bytes{'+selector+'}'
                    query=metric+' and (timestamp('+metric+') > time()-35)'
                    url='http://localhost:8080/prometheus/api/v1/query_range?'+urllib.parse.urlencode(dict(query=query,start=start,end=end,step=5))
                    with urllib.request.urlopen(url,timeout=15) as response:raw=json.load(response)
                    if raw['status']!='success':raise RuntimeError('Memory query unsuccessful')
                    buckets={}
                    for series in raw['data']['result']:
                        key=(series['metric']['pod'],series['metric']['container'])
                        for t,v in series['values']:
                            value=float(v)
                            if not math.isfinite(value):continue
                            row=buckets.setdefault(float(t),{})
                            if key in row:raise RuntimeError('Duplicate memory series')
                            row[key]=value
                    totals=[sum(row.values())/1048576 for row in buckets.values() if set(row)==set(expected)]
                    required=math.ceil((math.floor((end-start)/5)+1)*.8)
                    summary=dict(implementation=impl,sweep=attempt['pair'],memory_cap_mib=cap,phase=result_file.parent.name,source_result=str(result_file),source_sha256=hashlib.sha256(result_file.read_bytes()).hexdigest(),expected_containers=expected,pod_pin_provenance=provenance,observation_times=observation_times,start=start,end=end,complete=len(totals)>=required,samples=len(totals),required=required,median_mib=statistics.median(totals) if totals else None,peak_mib=max(totals) if totals else None,legacy_unfiltered_peak_mib=result.get('peak_application_memory_mib'),query=query,raw_series=raw['data']['result'])
                    target.parent.mkdir(parents=True,exist_ok=True);temp=target.with_suffix('.tmp');temp.write_text(json.dumps(summary,indent=2)+'\n');temp.replace(target)
                    summaries.append(summary)
                except Exception as e:
                    summaries.append(dict(implementation=impl,sweep=attempt['pair'],memory_cap_mib=cap,phase=result_file.parent.name,error=type(e).__name__+': '+str(e),complete=False))
    result=dict(policy='Fresh (<=35s) samples pinned to observed live pods; does not modify original allocation/latency/reliability/CPU decisions.',loads=[{k:v for k,v in s.items() if k!='raw_series'} for s in summaries])
    temp=ROOT/'memory-reconstructed-summary.tmp';temp.write_text(json.dumps(result,indent=2)+'\n');temp.replace(ROOT/'memory-reconstructed-summary.json')
    return result



