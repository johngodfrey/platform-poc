#!/usr/bin/env python3
"""Combine completed automatic-scaling trials, preserving failed-series provenance."""
import argparse
import copy
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('autoscale',ROOT/'run.py');auto=importlib.util.module_from_spec(spec);spec.loader.exec_module(auto)
POLICY_KEYS=['warmup','baseline','burst','recovery','baseline_rate','burst_rate','vus','window','down_hold','p95_ms','cpu_headroom','trigger','latency_settle']


def combine(sources,include_recovered=False):
    rows=[];origins=[];settings=None;hashes=None;counts=dict(java=0,wasm=0)
    for path,data in sources:
        recovered=data.get('operational_recovery')
        if not data.get('restored') or (data['status']!='complete' and not (include_recovered and recovered)):
            raise RuntimeError('Input must be complete/restored or explicitly verified recovered: '+str(path))
        defaults=dict(cpu_headroom=2,trigger='cpu',latency_settle=20)
        policy={key:data['settings'].get(key,defaults[key]) if key in defaults else data['settings'][key] for key in POLICY_KEYS}
        if settings is None:settings=policy;hashes=data['source_sha256']
        elif policy!=settings or hashes!=data['source_sha256']:raise RuntimeError('Policy or experiment-source hashes differ')
        for trial in data['trials']:
            if not trial.get('baseline_gate'):raise RuntimeError('Completed trial has no healthy-baseline evidence')
            if '[60s]' not in trial['cpu_query']:raise RuntimeError('Incompatible CPU sampling window')
            row=copy.deepcopy(trial);impl=row['implementation'];counts[impl]+=1
            row.update(pair=counts[impl],raw_pair=trial['pair'],source_report=str(Path(path).resolve()));rows.append(row)
        origins.append(dict(path=str(Path(path).resolve()),status=data['status'],restored=data['restored'],error=data.get('error'),operational_recovery=recovered))
    if counts['java']!=counts['wasm'] or not counts['java']:raise RuntimeError('Equal nonzero completed trial counts required')
    return dict(status='complete',restored=True,settings=dict(settings,pairs=counts['java'],target='both'),source_sha256=hashes,trials=rows,sources=origins,current='Completed matched trials; all source failures/recoveries retained below.')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directories',nargs='+',type=Path);parser.add_argument('--include-recovered',action='store_true');args=parser.parse_args()
    sources=[(p/'report.json',json.loads((p/'report.json').read_text())) for p in args.directories]
    data=combine(sources,args.include_recovered);directory=ROOT/'.results'/('comparison-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));directory.mkdir(parents=True)
    auto.report(directory,data);print(directory/'report.html')

if __name__=='__main__':main()
