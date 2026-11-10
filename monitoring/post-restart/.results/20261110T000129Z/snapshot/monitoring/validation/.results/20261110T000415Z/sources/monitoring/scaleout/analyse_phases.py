#!/usr/bin/env python3
"""Analyse matched request-start cohorts from a completed repeated scale-out series."""
import argparse
import csv
import html
import json
import math
from pathlib import Path
import statistics

CORE_SETTINGS=['warmup','baseline','before_scale','after_scale','recovery','baseline_rate','burst_rate','window','vus','p95_ms','factor','wasm_mode']


def phase_bounds(settings,results):
    step=settings['window']
    burst_start=settings['warmup']+settings['baseline']+1
    burst_end=burst_start+settings['before_scale']+settings['after_scale']
    before_start=math.ceil(burst_start/step)*step
    before_end=math.floor(min(r['scale_request_s'] for r in results)/step)*step
    ready=max(r['scale_accepted_s']+r['capacity_ready_seconds'] for r in results)
    after_start=math.ceil(ready/step)*step;after_end=math.floor(burst_end/step)*step
    if before_end<=before_start or after_end<=after_start:raise ValueError('Not enough full matched cohorts')
    return dict(before=(before_start,before_end),transition=(before_end,after_start),after=(after_start,after_end),cooldown=(math.ceil((burst_end+1)/step)*step,math.floor((burst_end+1+settings['recovery'])/step)*step))


def cohorts(metrics,start,end,step,rate,p95_limit):
    rows=[]
    for index in range(start//step,end//step):
        def count(name):return metrics[f'{name}{{window:{index}}}']['count']
        latency=metrics[f'scaleout_latency_ms{{window:{index}}}']
        n=count('scaleout_completed');accepted=count('scaleout_accepted');failures=count('scaleout_failures')
        if n<=0:raise ValueError('Empty cohort: '+str(index))
        if accepted+failures!=n:raise ValueError('Cohort order counters disagree')
        rows.append(dict(start_s=index*step,end_s=(index+1)*step,completed=n,accepted=accepted,failures=failures,mean_ms=latency['avg'],median_ms=latency['med'],p95_ms=latency['p(95)'],maximum_ms=latency['max'],passed=latency['p(95)']<p95_limit and failures==0 and accepted==n and accepted>=rate*step-1))
    return rows


def summarise(rows,rate):
    if not rows:raise ValueError('Cannot summarise an empty phase')
    seconds=sum(r['end_s']-r['start_s'] for r in rows);n=sum(r['completed'] for r in rows);accepted=sum(r['accepted'] for r in rows)
    return dict(seconds=seconds,completed=n,accepted=accepted,failures=sum(r['failures'] for r in rows),mean_latency_ms=sum(r['mean_ms']*r['completed'] for r in rows)/n,
                median_window_median_ms=statistics.median(r['median_ms'] for r in rows),median_window_p95_ms=statistics.median(r['p95_ms'] for r in rows),maximum_window_p95_ms=max(r['p95_ms'] for r in rows),accepted_orders_per_start_second=accepted/seconds,offered_orders_per_second=rate,nominal_offered_orders=rate*seconds,passing_windows=sum(r['passed'] for r in rows),windows=len(rows))


def analyse(root):
    series=json.loads((root/'series.json').read_text())
    if series['status']!='complete':raise ValueError('Series must be complete')
    selected=[p for p in series['pairs'] if p['valid'] and p['restored']]
    if not selected:raise ValueError('No valid restored pairs')
    inputs=[];reference=None
    for pair in selected:
        report=json.loads((root/pair['path']/'report.json').read_text());settings={k:report['settings'][k] for k in CORE_SETTINGS}
        if reference is None:reference=settings
        elif settings!=reference:raise ValueError('Load settings differ between pairs')
        for impl in ['java','wasm']:
            result=json.loads((root/pair['path']/impl/'result.json').read_text())
            if not result['target_capacity_stable_after_ready'] or result['capacity_ready_seconds'] is None:raise ValueError('Target capacity was not stable')
            metrics=json.loads((root/pair['path']/impl/'summary.json').read_text())['metrics']
            inputs.append((pair,impl,result,metrics))
    phases=phase_bounds(reference,[item[2] for item in inputs]);rows=[];windows=[]
    for pair,impl,result,metrics in inputs:
        for phase,(start,end) in phases.items():
            rate=reference['baseline_rate'] if phase=='cooldown' else reference['burst_rate']
            cells=cohorts(metrics,start,end,reference['window'],rate,reference['p95_ms'])
            row=dict(pair=pair['pair'],order=pair['order'],implementation=impl,phase=phase,start_s=start,end_s=end,**summarise(cells,rate));rows.append(row)
            windows.extend(dict(pair=pair['pair'],implementation=impl,phase=phase,**cell) for cell in cells)
    aggregate={}
    for phase in phases:
        aggregate[phase]={}
        for impl in ['java','wasm']:
            group=[r for r in rows if r['phase']==phase and r['implementation']==impl];stats={}
            for key in ['mean_latency_ms','median_window_median_ms','median_window_p95_ms','accepted_orders_per_start_second']:
                values=[r[key] for r in group];stats[key]=dict(median=statistics.median(values),minimum=min(values),maximum=max(values))
            aggregate[phase][impl]=dict(pairs=len(group),statistics=stats,passing_windows=sum(r['passing_windows'] for r in group),windows=sum(r['windows'] for r in group),accepted=sum(r['accepted'] for r in group),failures=sum(r['failures'] for r in group))
    comparisons=[]
    for pair in selected:
        pick=lambda impl,phase:next(r for r in rows if r['pair']==pair['pair'] and r['implementation']==impl and r['phase']==phase)
        comparisons.append(dict(pair=pair['pair'],java_lower_mean_before=pick('java','before')['mean_latency_ms']<pick('wasm','before')['mean_latency_ms'],java_lower_mean_after=pick('java','after')['mean_latency_ms']<pick('wasm','after')['mean_latency_ms'],java_lower_typical_window_p95_before=pick('java','before')['median_window_p95_ms']<pick('wasm','before')['median_window_p95_ms'],java_lower_typical_window_p95_after=pick('java','after')['median_window_p95_ms']<pick('wasm','after')['median_window_p95_ms']))
    cohort_comparisons={}
    for phase in phases:
        matched=[(j,next(w for w in windows if w['pair']==j['pair'] and w['implementation']=='wasm' and w['start_s']==j['start_s'])) for j in windows if j['phase']==phase and j['implementation']=='java']
        cohort_comparisons[phase]=dict(cohorts=len(matched),java_lower_p95=sum(j['p95_ms']<w['p95_ms'] for j,w in matched),java_lower_mean=sum(j['mean_ms']<w['mean_ms'] for j,w in matched))
    return dict(source_series=str(root.resolve()),selected_pairs=[p['pair'] for p in selected],excluded_pairs=[p['pair'] for p in series['pairs'] if p not in selected],settings=reference,phases=phases,rows=rows,windows=windows,aggregates=aggregate,comparisons=comparisons,cohort_comparisons=cohort_comparisons,
                methods=['Cohorts are grouped by request start; completion may occur after the cohort boundary.',
                         'Mean phase latency is weighted by the number of completed requests in each cohort. Aggregates take the median across per-run phase statistics.',
                         'Median window p95 is a typical 15-second cohort p95, not a pooled phase or request p95. Quantiles cannot be reconstructed from the stored summaries.',
                         'Phase counts do not reconstruct exact per-phase dropped_iterations; timing/scheduling boundaries can differ by a request. Raw full-run drop counters remain in the original reports.',
                         'Passing cohorts require p95 < limit, zero failures and accepted rate matching the offered rate, allowing one boundary request per cohort. Recovery uses a different two-consecutive-window rule.',
                         'Before is short (two full cohorts in this series); after is longer. No unscaled control run exists, so before/after changes do not establish that scaling caused them.',
                         'Java adds an order-service pod; Wasm adds a serving host. Cached artifacts, warmup, shared dependencies and order-data growth can influence results.'])


def write(root,data):
    output=root/'phase-analysis';output.mkdir(exist_ok=True)
    (output/'analysis.json').write_text(json.dumps(data,indent=2)+'\n')
    for name in ['rows','windows']:
        with (output/(name+'.csv')).open('w',newline='') as file:
            writer=csv.DictWriter(file,fieldnames=list(data[name][0]));writer.writeheader();writer.writerows(data[name])
    esc=lambda value:html.escape(str(value))
    fmt=lambda value:f'{value:,.1f}'
    page='<!doctype html><meta charset="utf-8"><title>Before / after scale-out analysis</title><style>body{font:17px system-ui;background:#101827;color:#eef;max-width:1400px;margin:35px auto;padding:15px}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{padding:9px;border-bottom:1px solid #456;text-align:left}.heat td{font-size:12px;text-align:center;padding:7px}.pass{background:#235641}.latency{background:#803b37}.delivery{background:#755a22}a{color:#8bc9ff}.scroll{overflow-x:auto}</style><h1>Before / after scale-out</h1>'
    page+='<p><a href="../report.html">Original series report and startup charts</a> · <a href="analysis.json">Detailed analysis</a> · <a href="rows.csv">Per-phase CSV</a> · <a href="windows.csv">Cohort CSV</a></p>'
    before_wins=sum(c['java_lower_mean_before'] for c in data['comparisons']);after_wins=sum(c['java_lower_mean_after'] for c in data['comparisons']);n=len(data['selected_pairs'])
    page+='<p>At '+str(data['settings']['burst_rate'])+' offered orders/s, Java had lower phase-mean latency before scaling in '+str(before_wins)+'/'+str(n)+' pairs and after readiness in '+str(after_wins)+'/'+str(n)+' pairs. This phase analysis is separate from first-order startup readiness.</p>'
    page+='<p>Java had lower individual-cohort p95 in '+str(data['cohort_comparisons']['before']['java_lower_p95'])+'/'+str(data['cohort_comparisons']['before']['cohorts'])+' before cohorts and '+str(data['cohort_comparisons']['after']['java_lower_p95'])+'/'+str(data['cohort_comparisons']['after']['cohorts'])+' after cohorts. Inspect every cohort below; neither implementation wins every window.</p>'
    page+='<p>Matched phases (seconds from each load start): '+esc(data['phases'])+'. The after phase begins after both added resources were observed ready in every trial; startup/transition is shown separately. Each value below is the median of five per-run phase statistics.</p>'
    page+='<table><tr><th>Phase</th><th>Application</th><th>Mean latency, ms</th><th>Typical 15s cohort p95, ms</th><th>Accepted orders / request-start second</th><th>Passing cohorts</th></tr>'
    for phase in data['phases']:
        for impl in ['java','wasm']:
            a=data['aggregates'][phase][impl];v=a['statistics'];cells=[phase,impl,fmt(v['mean_latency_ms']['median']),fmt(v['median_window_p95_ms']['median']),fmt(v['accepted_orders_per_start_second']['median']),str(a['passing_windows'])+'/'+str(a['windows'])]
            page+='<tr>'+''.join('<td>'+esc(c)+'</td>' for c in cells)+'</tr>'
    page+='</table><h2>Each pair: before → after</h2><table><tr><th>Pair / order</th><th>Application</th><th>Mean latency, ms</th><th>Typical cohort p95, ms</th><th>Accepted rate</th><th>Passing after cohorts</th></tr>'
    for pair in data['selected_pairs']:
        for impl in ['java','wasm']:
            before=next(r for r in data['rows'] if r['pair']==pair and r['implementation']==impl and r['phase']=='before');after=next(r for r in data['rows'] if r['pair']==pair and r['implementation']==impl and r['phase']=='after')
            change=lambda key:fmt(before[key])+' → '+fmt(after[key])
            cells=[str(pair)+' / '+before['order'],impl,change('mean_latency_ms'),change('median_window_p95_ms'),change('accepted_orders_per_start_second'),str(after['passing_windows'])+'/'+str(after['windows'])]
            page+='<tr>'+''.join('<td>'+esc(c)+'</td>' for c in cells)+'</tr>'
    page+='</table><h2>Cohort p95 heatmap (ms)</h2><p>Green meets both latency and delivery gates; red exceeds the latency limit; amber meets latency but misses the delivery gate. Hover for counts. These are individual cohort p95s.</p><div class="scroll"><table class="heat"><tr><th>Pair / app</th>'
    example=[r for r in data['windows'] if r['pair']==data['selected_pairs'][0] and r['implementation']=='java' and r['phase']!='cooldown']
    page+=''.join('<th>'+str(w['start_s'])+'–'+str(w['end_s'])+'<br>'+w['phase']+'</th>' for w in example)+'</tr>'
    for pair in data['selected_pairs']:
        for impl in ['java','wasm']:
            cells=[w for w in data['windows'] if w['pair']==pair and w['implementation']==impl and w['phase']!='cooldown'];page+='<tr><th>'+str(pair)+' / '+impl+'</th>'
            for w in cells:
                cls='pass' if w['passed'] else 'latency' if w['p95_ms']>=data['settings']['p95_ms'] else 'delivery'
                title=f"Accepted {w['accepted']}; completed {w['completed']}; failures {w['failures']}; mean {w['mean_ms']:.2f}ms"
                page+='<td class="'+cls+'" title="'+esc(title)+'">'+str(round(w['p95_ms']))+'</td>'
            page+='</tr>'
    page+='</table></div><h2>Method and limits</h2><ul>'+''.join('<li>'+esc(note)+'</li>' for note in data['methods'])+'</ul>'
    (output/'report.html').write_text(page)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('series',type=Path);args=parser.parse_args()
    output=write(args.series,analyse(args.series));print(output/'report.html')

if __name__=='__main__':main()
