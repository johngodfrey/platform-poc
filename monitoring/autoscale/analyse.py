#!/usr/bin/env python3
"""Render completed automatic-scaling trials with CPU, capacity and latency timelines."""
import argparse
import html
import json
import math
from pathlib import Path
import statistics


def derived(row,settings):
    burst=settings['warmup']+settings['baseline']+1;fall=burst+settings['burst']+1
    def difference(value,origin):return None if value is None else value-origin
    post=[c for c in row['cohorts'] if c['phase']=='burst' and row['up_serving_s'] is not None and c['start_s']>=row['up_serving_s']]
    if row['up_requested_s'] is None:outcome='No scale-out requested'
    elif len(post)<2:outcome='Insufficient complete post-capacity burst windows'
    elif row['burst_recovery_s'] is None:outcome='Post-capacity burst SLO not observed'
    else:outcome='Two passing post-capacity burst windows observed'
    burst_rows=[c for c in row['cohorts'] if c['phase']=='burst']
    return dict(burst_windows_passed=sum(c['passed'] for c in burst_rows),burst_windows_total=len(burst_rows),trigger_after_burst_s=difference(row['up_requested_s'],burst),direct_order_after_request_s=row.get('new_pod_order_accepted_seconds'),serving_after_request_s=row['up_startup_seconds'],down_complete_after_load_falls_s=difference(row['down_complete_s'],fall),automatic_cycle_completed=row['automatic_cycle_completed'],readiness_valid=row['readiness_valid'],dropped_iterations=row['dropped_iterations'],failures=row['failures'],post_capacity_burst_slo_s=row['burst_recovery_s'],post_capacity_burst_windows=len(post),slo_interpretation=outcome)


def timeline(row,settings):
    total=settings['warmup']+settings['baseline']+settings['burst']+settings['recovery']+2
    maximum=max([point['cores'] for point in row['cpu_samples']]+[row['cpu_target_cores']*2,1])
    x=lambda t:80+max(0,t)/total*960
    y=lambda value:210-max(0,value)/maximum*125
    svg=['<svg viewBox="0 0 1120 390" role="img" aria-label="CPU, serving capacity, and scaling event timeline">']
    burst=settings['warmup']+settings['baseline']+1;fall=burst+settings['burst']+1
    for start,end,colour,label in [(0,burst,'#26374d','100/s warm/baseline'),(burst,fall,'#532e36','400/s burst'),(fall,total,'#244239','100/s recovery')]:
        svg.append(f'<rect x="{x(start)}" y="40" width="{x(end)-x(start)}" height="300" fill="{colour}"/><text x="{x(start)+5}" y="32" fill="#eef">{label}</text>')
    for tick in range(6):
        t=total*tick/5;svg.append(f'<path d="M{x(t)} 40 V340" stroke="#556"/><text x="{x(t)}" y="365" text-anchor="middle" fill="#eef">{t:.0f}s</text>')
    svg.append('<text x="5" y="80" fill="#eef">CPU</text><text x="5" y="270" fill="#eef">Units</text>')
    points=' '.join(f'{x(p["elapsed"])},{y(p["cores"])}' for p in row['cpu_samples'])
    svg.append(f'<polyline points="{points}" fill="none" stroke="#6dbdff" stroke-width="3"/>')
    if row['cpu_target_cores']:
        for units in [1,2]:
            value=row['cpu_target_cores']*units;svg.append(f'<path d="M80 {y(value)} H1040" stroke="#d6c26e" stroke-dasharray="6 5"/><text x="1043" y="{y(value)+4}" fill="#eef">{value:.2f}</text>')
    def steps(up,down,colour,offset):
        coords=[(0,1)]
        if up is not None:coords.extend([(up,1),(up,2)])
        if down is not None:coords.extend([(down,2),(down,1)])
        coords.append((max(total,down or 0),1 if down is not None or up is None else 2))
        points=' '.join(f'{x(t)},{330-(v-1)*65+offset}' for t,v in coords)
        svg.append(f'<polyline points="{points}" stroke="{colour}" fill="none" stroke-width="3"/>')
    steps(row['up_requested_s'],row['down_requested_s'],'#e9bc61',-5)
    steps(row['up_serving_s'],row['down_complete_s'],'#a4df8c',0)
    svg.append('</svg><p>Blue: measured total CPU cores. Dashed: one/two calibrated CPU targets. Gold: desired units. Green: verified serving units. Each CPU point is a one-minute rate.</p>')
    return ''.join(svg)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);parser.add_argument('--allow-partial',action='store_true');args=parser.parse_args();source=json.loads((args.directory/'report.json').read_text())
    partial=source['status']!='complete' or not source['restored']
    if partial and not args.allow_partial:raise RuntimeError('Completed restored series required; --allow-partial explicitly retains incomplete status and recovery warnings')
    output=args.directory/'analysis';output.mkdir(exist_ok=True);settings=source['settings'];rows=[dict(pair=r['pair'],implementation=r['implementation'],**derived(r,settings)) for r in source['trials']]
    (output/'summary.json').write_text(json.dumps(dict(status=source['status'],restored=source['restored'],settings=settings,trials=rows,final_verification=source.get('final_verification')),indent=2)+'\n')
    esc=lambda x:html.escape(str(x));fmt=lambda x:'—' if x is None else f'{x:,.2f}'
    page='<!doctype html><meta charset="utf-8"><title>Automatic scaling comparison</title><style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1350px;margin:35px auto;padding:20px}svg{width:100%}td,th{padding:10px;border-bottom:1px solid #456;text-align:left}table{border-collapse:collapse;width:100%}a{color:#8bc9ff}</style><h1>Automatic scaling — paired comparison</h1>'
    if partial:page+='<p style="padding:20px;background:#702f32"><strong>Incomplete series. Only completed trials are plotted.</strong> Source status: '+esc(source['status'])+'. Restoration verified: '+str(source['restored'])+'. '+esc(source.get('error',''))+' '+esc(source.get('restoration_error',''))+'. See recovery instructions in the original report. Do not treat interrupted attempts as application performance results.</p>'
    for note in source.get('study_notes',[]):page+='<p style="padding:14px;background:#37445b">'+esc(note)+'</p>'
    page+='<p>Shared benchmark controller, not Kubernetes HPA. Identical policy with independently calibrated CPU targets. Java scales its order service; Wasm adds a whole serving host. Images/dependencies are warm. Decisions are measured separately from pod startup, HTTP admission and end-to-end SLO outcomes. Forced final cleanup is not automatic scale-in.</p>'
    page+='<p>CPU target factor: '+str(settings.get('cpu_headroom',2))+'× warmup median; up above 110% target. Burst: '+str(settings['burst'])+' seconds; recovery: '+str(settings['recovery'])+' seconds. These settings must match before pooling trials.</p>'
    page+='<p>Scale-out policy: '+esc(settings.get('trigger','cpu'))+'. Scale-in remains CPU-based. Latency mode requires two adjacent settled 15-second cohorts above '+str(settings['p95_ms'])+' ms, with a '+str(settings.get('latency_settle',20))+'-second settling delay and at least 100 completions per cohort. CPU mode uses 110% of its calibrated CPU target.</p>'
    page+='<h2>Policy outcomes</h2><table><tr><th>Application</th><th>Trials</th><th>Scale-out requests</th><th>Automatic 1→2→1 cycles</th><th>Passing complete burst windows</th><th>Failed orders</th><th>Dropped unsent iterations</th></tr>'
    for impl in ['java','wasm']:
        group=[r for r in source['trials'] if r['implementation']==impl]
        values=[impl,len(group),sum(r['up_requested_s'] is not None for r in group),sum(r['automatic_cycle_completed'] for r in group),str(sum(c['passed'] for r in group for c in r['cohorts'] if c['phase']=='burst'))+' / '+str(sum(c['phase']=='burst' for r in group for c in r['cohorts'])),sum(r['failures'] for r in group),sum(r['dropped_iterations'] for r in group)]
        page+='<tr>'+''.join('<td>'+esc(value)+'</td>' for value in values)+'</tr>'
    page+='</table><p>Judge scaling actions alongside passing SLO windows and accepted throughput. When the latency trigger never fires, no startup time or automatic cycle is measured.</p><h2>Every completed trial</h2>'
    page+='<table><tr><th>Pair</th><th>App</th><th>Trigger after burst, s</th><th>New-pod order after request, s</th><th>Serving after request, s</th><th>Scale-in complete after load falls, s</th><th>Automatic cycle</th><th>Readiness valid</th><th>Dropped / failed</th></tr>'
    for r in rows:
        readiness='not requested' if r['trigger_after_burst_s'] is None else r['readiness_valid']
        values=[r['pair'],r['implementation'],fmt(r['trigger_after_burst_s']),fmt(r['direct_order_after_request_s']),fmt(r['serving_after_request_s']),fmt(r['down_complete_after_load_falls_s']),r['automatic_cycle_completed'],readiness,f'{r["dropped_iterations"]} / {r["failures"]}']
        page+='<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>'
    page+='</table>'
    for row in source['trials']:
        page+=f'<h2>Pair {row["pair"]} — {row["implementation"]}</h2>'+timeline(row,settings)
        interpretation=derived(row,settings)
        page+='<p>'+esc(interpretation['slo_interpretation'])+'. Complete post-capacity burst cohorts: '+str(interpretation['post_capacity_burst_windows'])+'.</p>'
        page+='<p>CPU target: '+fmt(row['cpu_target_cores'])+' core/unit. Telemetry/calibration waits: '+str(len(row['signal_errors']))+'. Post-capacity burst SLO observed at: '+fmt(row['burst_recovery_s'])+' seconds from load start. A healthy post-capacity window does not by itself prove scaling caused recovery.</p>'
        if settings.get('trigger')=='latency' and row['up_requested_s'] is not None:
            signals={item['window']:item for item in row.get('latency_samples',[]) if item['observed_s']<=row['up_requested_s']}
            trigger=[signals[key] for key in sorted(signals)[-2:]]
            page+='<p>Settled cohorts immediately preceding scale-out: '+esc('; '.join(str(item['window'])+' (ends '+str(item['end_s'])+'s), p95 '+fmt(item['p95_ms'])+' ms, observed at '+fmt(item['observed_s'])+'s' for item in trigger))+'.</p>'
        errors=row['readiness']['errors']
        if errors:
            page+='<p>Readiness observer errors: '+str(len(errors))+'. First at '+fmt(errors[0]['time']-row['start_time'])+' seconds from load start. The strict readiness-valid flag remains false even if the first serving event preceded these later errors; raw errors are retained.</p>'
        if row.get('timing_correction'):
            page+='<p>Serving time recovered from the first ready event in the original observation log. A queued scale-in overwrote the summary timestamp; the original summary is retained alongside the corrected result. Scaling actions and traffic were unchanged.</p>'
        page+='<table><tr><th>Request-start cohort, s</th><th>Phase</th><th>Median, ms</th><th>p95, ms</th><th>Accepted</th><th>Failed</th><th>SLO passed</th></tr>'
        for c in row['cohorts']:
            if c['phase']=='transition/warmup':continue
            values=[f'{c["start_s"]}–{c["end_s"]}',c['phase'],fmt(c['median_ms']),fmt(c['p95_ms']),c['accepted'],c['failures'],c['passed']]
            page+='<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>'
        page+='</table>'
    verification=source.get('final_verification')
    if verification:
        page+='<h2>Post-test recovery verification</h2><p>Original application images, templates, replica counts, HostConfig, version and component/provider counts were verified. These 30-second 100/s smoke tests check availability, not the benchmark latency gate.</p><table><tr><th>Stage</th><th>Application</th><th>Accepted</th><th>Failed</th><th>Dropped</th><th>p95 ms</th><th>p95 below 500 ms</th></tr>'
        stages=[('Final',verification['smoke'])]
        if verification.get('smoke_before_recovery'):stages=[('Before recovery',verification['smoke_before_recovery']),('After runtime recovery',verification['smoke'])]
        for stage,healthchecks in stages:
            for impl,health in healthchecks.items():
                values=[stage,impl,health['accepted'],health['failed_checks'],health['dropped'],fmt(health['p95_ms']),health['p95_ms']<settings['p95_ms']]
                page+='<tr>'+''.join('<td>'+esc(value)+'</td>' for value in values)+'</tr>'
        page+='</table><p>Successful configuration restoration and accepted orders do not establish a stable pre-test latency baseline.</p>'
        slow=[impl for impl,health in verification['smoke'].items() if health['p95_ms']>=settings['p95_ms']]
        if slow:page+='<p>Final smoke exceeded the latency SLO: '+esc(', '.join(slow))+'. That limitation is retained.</p>'
    page+='<h2>Source evidence</h2>'
    for note in source.get('post_test_notes',[]):page+='<p>'+esc(note)+'</p>'
    for origin in source.get('sources',[]):
        path=Path(origin['path']);page+='<p><a href="'+html.escape(str(path.parent/'report.html'),quote=True)+'">'+esc(path.parent.name)+'</a>: '+esc(origin['status'])+'; restored '+str(origin['restored'])+'. '+esc(origin.get('error') or '')+' '+esc(origin.get('operational_recovery') or '')+'</p>'
    if source.get('diagnostic_attempts'):
        page+='<h2>Excluded diagnostic/preflight attempts</h2>'
        for attempt in source['diagnostic_attempts']:
            page+='<p><a href="'+html.escape(attempt['report'],quote=True)+'">'+esc(attempt['name'])+'</a>: '+esc(attempt['reason'])+'</p>'
    page+='<p>Raw evidence and full settings: <a href="../report.html">original report</a>, ../report.json and per-trial observations/readiness files in the source folders. A missing event is shown as —, not zero. All events and failures remain visible; no pair is silently discarded.</p>'
    (output/'report.html').write_text(page);print(output/'report.html')

if __name__=='__main__':main()
