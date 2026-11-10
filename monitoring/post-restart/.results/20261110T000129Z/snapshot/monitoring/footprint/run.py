#!/usr/bin/env python3
"""Run a sleep-inhibited footprint demo, exporting live metrics to local Prometheus."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('implementation', choices=['java', 'wasm', 'both'])
parser.add_argument('--idle', type=int, default=30)
parser.add_argument('--load', type=int, default=120)
parser.add_argument('--recovery', type=int, default=30)
parser.add_argument('--rate', type=int, default=100)
args = parser.parse_args()
if min(args.idle, args.load, args.recovery, args.rate) < 1:
    parser.error('durations and rate must be positive')
if not shutil.which('k6'):
    parser.error('k6 must be on PATH')
environment = dict(os.environ, K6_PROMETHEUS_RW_SERVER_URL='http://localhost:8080/prometheus/api/v1/write',
                   K6_PROMETHEUS_RW_TREND_STATS='p(95),avg,max', K6_PROMETHEUS_RW_STALE_MARKERS='true')
sleep_guard = subprocess.Popen(['caffeinate', '-i', '-s']) if sys.platform == 'darwin' else None
try:
    for implementation in (['java', 'wasm'] if args.implementation == 'both' else [args.implementation]):
        run_id = implementation + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = ROOT / '.results' / run_id
        output.mkdir(parents=True)
        command = ['k6', 'run', '--quiet', '-o', 'experimental-prometheus-rw',
                   '--summary-export=' + str(output / 'summary.json'),
                   '-e', 'IMPLEMENTATION=' + implementation, '-e', 'RUN_ID=' + run_id,
                   '-e', 'IDLE_SECONDS=' + str(args.idle), '-e', 'LOAD_SECONDS=' + str(args.load),
                   '-e', 'RECOVERY_SECONDS=' + str(args.recovery), '-e', 'ORDER_RATE=' + str(args.rate),
                   str(ROOT / 'footprint.js')]
        print(f'\n{implementation}: {args.idle}s idle → {args.load}s load → {args.recovery}s recovery', flush=True)
        print('Dashboard: http://localhost:8080/grafana/d/java-wasm-footprint', flush=True)
        with (output / 'k6.log').open('w') as log:
            process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            try:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    log.write(line)
                code = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if code:
            sys.exit(code)
finally:
    if sleep_guard:
        sleep_guard.terminate()
        sleep_guard.wait()
