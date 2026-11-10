#!/usr/bin/env python3
"""Build an offline visual summary of one completed, matched throughput series."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path

FIELDS = ('seconds', 'warmup', 'warmup_rate', 'p95_ms', 'vus', 'confirm_seconds')


def signature(report):
    return ([report['settings'][k] for k in FIELDS], report['budget'])


def checked_source(path):
    path = path.resolve()
    report = json.loads(path.read_text())
    if not all(report.get(k) for k in ('completed', 'restored', 'applications_healthy')):
        raise ValueError('Incomplete or unrestored source report')
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), report=report)


def load_reports(directory, extra_paths=()):
    series = json.loads((directory/'series.json').read_text())
    if series['state'] != 'completed':
        raise ValueError('Series is not completed; retain pending or failed evidence separately')
    if len(series['runs']) != 2:
        raise ValueError('Exactly two child runs required; do not silently pool extra attempts')
    reports = {}
    protocol = None
    for row in series['runs']:
        if len(row['reports']) != 1:
            raise ValueError('One source report per child required')
        path = Path(row['reports'][0])/'report.json'
        source = checked_source(path)
        current = signature(source['report'])
        if protocol is not None and current != protocol:
            raise ValueError('Source budgets or measurement protocols differ')
        protocol = current
        reports[row['implementation']] = source
    if set(reports) != {'java', 'wasm'}:
        raise ValueError('Exactly one Java and one Wasm source required')
    seen = {source['path'] for source in reports.values()}
    for path in extra_paths:
        source = checked_source(path/'report.json' if path.is_dir() else path)
        if signature(source['report']) != protocol:
            raise ValueError('Follow-up budgets or measurement protocols differ')
        implementations = list(source['report']['implementations'])
        if len(implementations) != 1 or implementations[0] not in reports:
            raise ValueError('Follow-up must identify exactly one implementation')
        if source['path'] in seen:
            raise ValueError('Duplicate source report would double-count evidence')
        seen.add(source['path'])
        reports[implementations[0]].setdefault('extras', []).append(source)
    return reports


def confirmed(report, implementation):
    row = report['implementations'][implementation].get('confirmation')
    return row if row and row.get('passed') and row.get('confirmation') and row.get('seconds', 0) >= 300 else None


def best_confirmed(source, implementation):
    candidates = [confirmed(s['report'], implementation) for s in [source]+source.get('extras', [])]
    candidates = [c for c in candidates if c is not None]
    return max(candidates, key=lambda c:c['rate']) if candidates else None


def render(directory, reports):
    esc = lambda value: html.escape(str(value))
    settings = reports['java']['report']['settings']
    budget = reports['java']['report']['budget']
    out = '<!doctype html><meta charset="utf-8"><title>Java and Wasm sustainable throughput</title><style>body{font:18px system-ui;background:#101827;color:#eef;max-width:1200px;margin:35px auto;padding:24px}a{color:#9cd}table{border-collapse:collapse;width:100%}th,td{padding:12px;text-align:left;border-bottom:1px solid #456}.bar{height:34px;display:inline-block;vertical-align:middle;margin:10px 14px 10px 0}small{color:#ccd}</style><h1>Sustainable accepted-order throughput</h1>'
    out += '<p>'+str(budget['cpu_millicores']/1000)+' application CPU cores and '+str(budget['memory_mib'])+' MiB each; p95 &lt; '+str(settings['p95_ms'])+' ms; zero errors or dropped arrivals; '+str(settings['vus'])+' order workers.</p><h2>Five-minute confirmations</h2>'
    started = json.loads((directory/'series.json').read_text()).get('started')
    if started is not None:
        out += '<p><a href="http://localhost:8080/grafana/d/java-wasm-capacity?from='+str(int(started*1000))+'&amp;to=now">Grafana: this complete comparison time range</a></p>'
    maximum = max([best_confirmed(r, impl)['rate'] for impl, r in reports.items() if best_confirmed(r, impl)]+[1])
    for impl, color in [('java', '#5794f2'), ('wasm', '#73bf69')]:
        row = best_confirmed(reports[impl], impl)
        rate = row['rate'] if row else 0
        out += '<p><b>'+impl+'</b><br><span class="bar" style="background:'+color+';width:'+str(70*rate/maximum)+'%"></span>'+(str(rate)+' orders/s · p95 '+str(round(row['p95_ms'], 2))+' ms · '+str(row['accepted'])+' accepted' if row else 'No passing five-minute confirmation')+'</p>'
    out += '<p>These are confirmed tested bounds from a sequential comparison and any listed follow-ups, not exact maxima or repeated-run confidence estimates. Different grids can leave overlapping capacity brackets; different confirmed bounds alone do not prove different true maxima. Java divides its budget equally across four services; Wasm pools its budget in one runtime. Shared dependencies, sidecars, ingress and monitoring are outside application limits. This compares these allocations, not intrinsic language performance.</p><h2>Every measured step</h2><table><tr><th>Application</th><th>Phase</th><th>Offered/s</th><th>Accepted/s</th><th>p95 ms</th><th>Failures</th><th>Dropped</th><th>Outcome</th></tr>'
    for impl in ('java', 'wasm'):
        for index, source in enumerate([reports[impl]]+reports[impl].get('extras', [])):
            data = source['report']['implementations'][impl]
            rows = [('Two-minute step', r) for r in data['stages']]
            if data.get('confirmation'):
                rows.append(('Confirmation', data['confirmation']))
            for phase, row in rows:
                values = [impl, ('Follow-up ' if index else '')+phase, row['rate'], round(row['achieved_orders_per_second'], 2), round(row['p95_ms'], 2), row['failures'], row['dropped'], row['status']]
                out += '<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>'
            link = os.path.relpath(Path(source['path']).with_name('report.html'), directory)
            out += '<tr><td colspan="8"><a href="'+esc(link)+'">'+impl+' raw report, summaries, logs and resource samples</a></td></tr>'
    return out+'</table><p>Short passing stages are not promoted into confirmed sustained rates. All failed stages remain shown. A failed highest-step confirmation requires another bounded test at a lower rate. Dropped arrivals are inconclusive about an application ceiling. Application resources and functional checks were restored after each child run.</p><p><a href="series.json">Series journal</a> · <a href="comparison.json">Derived data and source hashes</a></p>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('series', type=Path)
    parser.add_argument('--extra-report', type=Path, action='append', default=[], help='Retain a matched single-implementation boundary follow-up, including failed steps')
    args = parser.parse_args()
    directory = args.series.resolve()
    reports = load_reports(directory, args.extra_report)
    (directory/'comparison.json').write_text(json.dumps(dict(kind='derived_summary', sources=reports, confirmed_bounds={i:best_confirmed(r,i) for i,r in reports.items()}), indent=2)+'\n')
    (directory/'comparison.html').write_text(render(directory, reports))
    print(directory/'comparison.html')


if __name__ == '__main__':
    main()
