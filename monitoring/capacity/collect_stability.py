#!/usr/bin/env python3
"""Archive supplemental stability trends without changing original reports or loads."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request


def read(path):return json.loads(Path(path).read_text())

def query(expression,start,end):
    url='http://localhost:8080/prometheus/api/v1/query_range?'+urllib.parse.urlencode(dict(query=expression,start=start,end=end,step=15))
    with urllib.request.urlopen(url,timeout=20) as response:data=json.load(response)
    if data.get('status')!='success':raise RuntimeError('Prometheus query failed')
    return data

def main(directory):
    journal=read(directory/'validation.json');addendum=read(directory/'stability-addendum.json')
    if journal['state']!='completed' or addendum['state']!='completed':raise RuntimeError('Wait for both studies to finish')
    for job in journal['jobs']+addendum['jobs']:
        if not job['name'].startswith('stability-'):continue
        report_path=Path(job['report']);report=read(report_path)
        if not all(report.get(k) for k in ['completed','restored','applications_healthy']):raise RuntimeError('Long study needs recovery')
        if report['settings']['seconds']!=1800:raise RuntimeError('Expected a 30-minute study')
        for impl,data in report['implementations'].items():
            for stage in data['stages']:
                folder=report_path.parent/(impl+'-'+str(stage['rate']))
                original=folder/'telemetry.json';telemetry=read(original)
                output=folder/'stability-trends.json'
                if output.exists():
                    print('Retaining existing observation:',output);continue
                start,end=telemetry['load_start'],telemetry['load_end']
                testid='capacity-'+report_path.parent.name+'-'+folder.name
                labels='namespace="'+impl+'",pod=~"'+'|'.join(telemetry['current_pods'])+'",container!="",container!="POD"'
                expressions=dict(cpu90='sum by(pod,container)(rate(container_cpu_usage_seconds_total{'+labels+'}[90s]))',
                                 latency_p95_cumulative='k6_http_req_duration_p95{testid="'+testid+'",scenario="orders"}',
                                 latency_max_cumulative='k6_http_req_duration_max{testid="'+testid+'",scenario="orders"}',
                                 accepted='sum(k6_demo_orders_accepted_total{testid="'+testid+'"})',
                                 failures='sum(k6_demo_order_failures_total{testid="'+testid+'"})')
                result=dict(kind='supplemental_observation',collected=time.time(),original_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
                            source_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),expressions=expressions,queries={},
                            note='90-second CPU rates tolerate sparse idle-container samples. k6 latency gauges are cumulative within this stage, not rolling quantiles. Final counts/gates come from the original k6 summary.')
                for name,expression in expressions.items():result['queries'][name]=query(expression,start,end)
                output.write_text(json.dumps(result,indent=2)+'\n');print(output)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);args=parser.parse_args();main(args.directory)
