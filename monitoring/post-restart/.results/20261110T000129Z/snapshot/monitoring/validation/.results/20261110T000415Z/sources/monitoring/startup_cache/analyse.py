#!/usr/bin/env python3
"""Combine only complete valid cold/cached pairs; preserve exclusions and source links."""
import argparse
import copy
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('startup_cache',ROOT/'run.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


def select_pairs(sources):
    selected=[];excluded=[];counts=dict(java=0,wasm=0)
    for source,data in sources:
        groups={}
        for row in data['trials']:groups.setdefault((row['implementation'],row['pair']),[]).append(row)
        for (implementation,pair),rows in groups.items():
            valid=(len(rows)==2 and {row['cache_mode'] for row in rows}=={'cold','cached'} and all(row['valid'] for row in rows))
            if not valid:
                excluded.append(dict(source=str(source),implementation=implementation,pair=pair,reason='Incomplete or invalid member; exclude both conditions'))
                continue
            counts[implementation]+=1
            for row in rows:
                item=copy.deepcopy(row);item.update(pair=counts[implementation],raw_pair=pair,source_report=str(source));selected.append(item)
    return selected,excluded


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directories',nargs='+',type=Path);args=parser.parse_args()
    sources=[(directory/'report.json',json.loads((directory/'report.json').read_text())) for directory in args.directories]
    if any(data['status']!='complete' or not data['restored'] for _,data in sources):raise RuntimeError('All input runs must be complete and restored')
    selected,excluded=select_pairs(sources)
    directory=ROOT/'.results'/('comparison-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));directory.mkdir(parents=True)
    data=dict(directory=str(directory),status='complete',restored=True,current='Only complete valid pairs included; observer failures retained in raw reports.',trials=selected,excluded_pairs=excluded,source_reports=[str(p.parent/'report.html') for p,_ in sources])
    r.report(data);print(directory/'report.html')

if __name__=='__main__':main()
