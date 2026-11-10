#!/usr/bin/env python3
"""Alternate five matched Java/Wasm scale-out pairs and show every result."""
import argparse
from datetime import datetime,timezone
import hashlib
import html
import json
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
SOURCES=['run.py','readiness.py','host_mode.py','load.js','repeat.py']
METRICS=[('new_pod_ready_seconds','New pod Ready (s)'),('new_pod_order_accepted_seconds','Direct order accepted (s)'),('capacity_ready_seconds','Full capacity observed after acknowledgement (s)'),('median_ms','Whole-run median latency (ms)'),('p95_ms','Whole-run p95 latency (ms)'),('dropped_iterations','Dropped iterations'),('failures','Failed orders')]


def save(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path)


def baseline(state):
    return dict(java={k:state['java'].get(k) for k in ['image','resources','original','source_template_hash','images']},
                wasm=dict(version=state['wasm']['version'],template=state['wasm']['host']['source_template_hash'],images=state['wasm']['host']['images'],components=state['wasm']['host']['components']))


def collect(directory,number,order):
    report=json.loads((directory/'report.json').read_text());state=json.loads((directory/'state.json').read_text())
    row=dict(pair=number,order=order,path=directory.name,restored=report.get('restored',False),baseline=baseline(state),implementations={})
    for impl,result in report['implementations'].items():
        metrics=json.loads((directory/impl/'summary.json').read_text())['metrics']
        row['implementations'][impl]=dict(readiness_valid=result.get('readiness_comparison_valid',False),new_pod_ready_seconds=result.get('new_pod_ready_seconds'),new_pod_order_accepted_seconds=result.get('new_pod_order_accepted_seconds'),capacity_ready_seconds=result.get('capacity_ready_seconds'),median_ms=metrics['scaleout_latency_ms']['med'],p95_ms=metrics['scaleout_latency_ms']['p(95)'],dropped_iterations=result['dropped_iterations'],failures=metrics['scaleout_failures']['count'],accepted=metrics['scaleout_accepted']['count'],already_healthy=result['already_healthy_before_scale'],recovery_seconds=result['recovery_seconds'],target_stable=result['target_capacity_stable_after_ready'])
    row['valid']=row['restored'] and len(row['implementations'])==2 and all(r['readiness_valid'] and r['target_stable'] for r in row['implementations'].values())
    return row


def aggregates(series):
    valid=[row for row in series['pairs'] if row.get('valid')]
    values={}
    for impl in ['java','wasm']:
        values[impl]={}
        for key,_ in METRICS:
            points=[row['implementations'][impl][key] for row in valid if row['implementations'][impl].get(key) is not None]
            values[impl][key]=dict(n=len(points),median=statistics.median(points) if points else None,minimum=min(points) if points else None,maximum=max(points) if points else None)
    return values


def readiness_plot(series):
    key='new_pod_order_accepted_seconds'
    rows=[r for r in series['pairs'] if r.get('valid')];points=[r['implementations'][impl][key] for r in rows for impl in ['java','wasm']]
    if not points:return '<p>Waiting for completed, valid pairs.</p>'
    maximum=max(points)*1.15 or 1
    def x(value):return 130+value/maximum*640
    svg=['<svg viewBox="0 0 850 190" role="img" aria-label="Individual direct-order readiness times for Java and Wasm">']
    for tick in range(6):
        value=maximum*tick/5;px=x(value);svg.append(f'<path d="M{px} 20 V150" stroke="#465064"/><text x="{px}" y="180" text-anchor="middle" fill="white">{value:.1f}s</text>')
    for impl,y,color in [('java',55,'#5794f2'),('wasm',120,'#73bf69')]:
        svg.append(f'<text x="10" y="{y+5}" fill="white">{impl}</text>')
        for i,row in enumerate(rows):
            value=row['implementations'][impl][key];svg.append(f'<circle cx="{x(value)}" cy="{y+(i-(len(rows)-1)/2)*8}" r="5" fill="{color}"><title>Pair {row["pair"]}: {value:.2f}s</title></circle>')
        median=statistics.median(row['implementations'][impl][key] for row in rows);svg.append(f'<path d="M{x(median)} {y-25} V{y+25}" stroke="{color}" stroke-width="3"/>')
    return ''.join(svg)+'</svg><p>Each dot is one run; vertical bars are medians. Timings start when the scale request begins.</p>'


def write_report(directory,series):
    series['aggregates']=aggregates(series);save(directory/'series.json',series)
    esc=lambda v:html.escape(str(v))
    fmt=lambda value:'—' if value is None else f'{value:,.2f}'
    page='<!doctype html><meta charset="utf-8"><title>Repeated scale-out comparison</title>'
    if series['status']=='running':page+='<meta http-equiv="refresh" content="15">'
    page+='<style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1300px;margin:40px auto;padding:20px}td,th{padding:10px;border-bottom:1px solid #456;text-align:left}table{border-collapse:collapse}svg{max-width:100%}a{color:#8bc9ff}</style>'
    page+='<h1>Repeated Java / Wasm scale-out comparison</h1><p>Status: '+esc(series['status'])+'. '+esc(len(series['pairs']))+' / '+esc(series['settings']['pairs'])+' attempted pairs completed. '+esc(sum(r.get('valid',False) for r in series['pairs']))+' valid pairs.</p>'
    page+='<p>Identical independent 2-second pod polling and direct new-pod order probes (HTTP 201 plus application success body, 10-second timeout) for both. New-pod times start at scale request initiation, not acknowledgement. Wasm full-host inventory/routing checks remain separate. Java adds only order-service; Wasm adds a serving host and its providers/components. Images may be cached; Kubernetes Ready probes still differ between applications. Results are observed timings, including polling/probe delays. No retries of benchmark orders.</p>'
    page+='<p>Current pair: '+esc(series.get('current_pair','—'))+'; order: '+esc(series.get('current_order','—'))+'</p>'
    page+=readiness_plot(series)
    page+='<table><tr><th>Metric</th><th>Java median [min–max]</th><th>Wasm median [min–max]</th></tr>'
    for key,title in METRICS:
        page+='<tr><td>'+esc(title)+'</td>'
        for impl in ['java','wasm']:
            a=series['aggregates'][impl][key];page+='<td>'+fmt(a['median'])+' ['+fmt(a['minimum'])+'–'+fmt(a['maximum'])+']</td>'
        page+='</tr>'
    page+='</table><p>Each latency value above summarises separate run statistics; the median of run p95s is not a pooled request p95. Warmup, burst and cooldown all contribute to the whole-run latency values. Invalid pairs remain visible and are excluded as complete pairs from aggregates. There is no database reset between trials.</p>'
    page+='<h2>Every run</h2><table><tr><th>Pair / order</th><th>Application</th><th>Valid pair</th><th>Pod Ready, s</th><th>Direct order, s</th><th>Capacity after ack, s</th><th>p95, ms</th><th>Dropped</th><th>Failures</th><th>Already healthy</th><th>Recovery, s</th></tr>'
    for row in series['pairs']:
        for impl,r in row.get('implementations',{}).items():
            cells=[f'<a href="{esc(row["path"])}/report.html">{row["pair"]} / {esc(row["order"])}</a>',esc(impl),esc(row.get('valid')),fmt(r['new_pod_ready_seconds']),fmt(r['new_pod_order_accepted_seconds']),fmt(r['capacity_ready_seconds']),fmt(r['p95_ms']),esc(r['dropped_iterations']),esc(r['failures']),esc(r['already_healthy']),fmt(r['recovery_seconds'])]
            page+='<tr>'+''.join('<td>'+cell+'</td>' for cell in cells)+'</tr>'
    page+='</table><p>'+esc(series.get('error',''))+'</p>'
    tmp=directory/'report.html.tmp';tmp.write_text(page);tmp.replace(directory/'report.html')


def stop_child(child):
    if child and child.poll() is None:
        child.send_signal(signal.SIGINT)
        try:child.wait(timeout=360)
        except subprocess.TimeoutExpired:raise RuntimeError('Runner did not stop/restore within 360s; use its saved state.json for recovery')


def power_snapshot():
    if sys.platform!='darwin':return dict(ac=None,reason='not macOS')
    result=subprocess.run(['pmset','-g','batt'],capture_output=True,text=True,timeout=10)
    return dict(time=time.time(),ac=result.returncode==0 and "'AC Power'" in result.stdout,details=result.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pairs',type=int,default=5)
    parser.add_argument('--output')
    for name,default in [('burst-rate',400),('after-scale',180),('warmup',60),('baseline',30),('before-scale',45),('recovery',30),('window',15),('vus',250)]:parser.add_argument('--'+name,type=int,default=default)
    args=parser.parse_args()
    if min(v for k,v in vars(args).items() if k!='output')<=0:parser.error('positive settings required')
    if args.before_scale<3*args.window or args.after_scale<3*args.window:parser.error('at least three pre/post window lengths required')
    directory=Path(args.output).resolve() if args.output else ROOT/'.results'/('repeated-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    directory.mkdir(parents=True)
    sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}
    (directory/'source').mkdir()
    for name in SOURCES:(directory/'source'/name).write_bytes((ROOT/name).read_bytes())
    series=dict(status='running',settings=vars(args),sources=sources,pairs=[]);write_report(directory,series)
    guard=subprocess.Popen(['caffeinate','-i','-s']) if sys.platform=='darwin' else None
    child=None
    try:
        print('Series report:',directory/'report.html',flush=True)
        for number in range(1,args.pairs+1):
            if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=value for name,value in sources.items()):raise RuntimeError('Benchmark source changed during the series')
            power_before=power_snapshot()
            save(directory/f'pair-{number:02d}-power-before.json',power_before)
            if power_before['ac'] is False:raise RuntimeError('Mains power is required for this series; no new load started')
            order='java-first' if number%2 else 'wasm-first';pair=directory/f'pair-{number:02d}'
            series.update(current_pair=number,current_order=order);write_report(directory,series)
            cmd=[sys.executable,str(ROOT/'run.py'),'run','both','--wasm-mode','host','--order',order,'--output',str(pair)]
            for name,value in vars(args).items():
                if name not in ['pairs','output']:cmd+=['--'+name.replace('_','-'),str(value)]
            print(f'Pair {number}/{args.pairs}, {order}',flush=True)
            with (directory/f'pair-{number:02d}.log').open('w') as log:
                child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
                seconds=args.warmup+args.baseline+1+args.before_scale+args.after_scale+1+args.recovery
                try:code=child.wait(timeout=2*(seconds+360))
                except subprocess.TimeoutExpired:stop_child(child);raise RuntimeError('Bounded pair timed out')
            if not (pair/'report.json').exists():raise RuntimeError(f'Pair {number} failed before reporting; inspect its log')
            row=collect(pair,number,order)
            power_after=power_snapshot();save(directory/f'pair-{number:02d}-power-after.json',power_after)
            row.update(power_before=power_before,power_after=power_after)
            if power_after['ac'] is False:row.update(valid=False,power_changed=True)
            if series['pairs'] and row['baseline']!=series['pairs'][0]['baseline']:row.update(valid=False,baseline_changed_between_pairs=True)
            series['pairs'].append(row);write_report(directory,series)
            print('Completed pair',number,'valid:',row['valid'],'restored:',row['restored'],flush=True)
            if power_after['ac'] is False:raise RuntimeError('Mains power disconnected; stopping after workload restoration')
            if code or not row['restored']:raise RuntimeError(f'Pair {number} failed or did not restore; stopping the series')
        series['status']='complete'
    except BaseException as error:
        series.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error));stop_child(child);raise
    finally:
        write_report(directory,series)
        if guard:guard.terminate();guard.wait()
        print('Series report:',directory/'report.html',flush=True)

if __name__=='__main__':main()
