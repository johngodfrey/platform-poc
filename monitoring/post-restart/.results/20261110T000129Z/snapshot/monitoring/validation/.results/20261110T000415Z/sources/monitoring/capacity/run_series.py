#!/usr/bin/env python3
"""Refine Java and Wasm throughput bounds with continuous inter-run sleep protection."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.replace(path)


def main():
    # Start protection before preflight, and retain it across both child runs.
    guard = subprocess.Popen(['caffeinate', '-i', '-s']) if sys.platform == 'darwin' else None
    child = None
    directory = ROOT/'.results'/('series-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    directory.mkdir(parents=True)
    status = dict(started=time.time(), state='running', runs=[])
    try:
        print('Series:', directory, flush=True)
        for implementation, rates in [('java', '25,50,75,100,150,200,300,400'),
                                      ('wasm', '200,250,300,350,400')]:
            existing = set((ROOT/'.results').iterdir())
            command = [sys.executable, str(ROOT/'run.py'), implementation,
                '--budget', str(ROOT.parent/'budget/budget.json'), '--rates', rates,
                '--seconds', '120', '--warmup', '180', '--warmup-rate', '25',
                '--p95-ms', '100', '--vus', '250', '--confirm-seconds', '300']
            row = dict(implementation=implementation, command=command, state='running')
            status['runs'].append(row);save(directory/'series.json', status)
            print('Starting', implementation, 'rates', rates, flush=True)
            with (directory/(implementation+'.log')).open('w') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic()+3600
                while child.poll() is None:
                    if time.monotonic()>deadline:
                        raise RuntimeError('Bounded child execution exceeded one hour')
                    time.sleep(1)
                row['exit_code'] = child.returncode
            row['reports'] = [str(p) for p in sorted(set((ROOT/'.results').iterdir())-existing)]
            row['state'] = 'completed' if child.returncode == 0 else 'failed'
            save(directory/'series.json', status)
            if child.returncode:
                raise RuntimeError(implementation+' driver failed; retain logs and restore its checkpoint before proceeding')
            if len(row['reports']) != 1:
                raise RuntimeError('Expected one uniquely owned child report')
            report = json.loads((Path(row['reports'][0])/'report.json').read_text())
            if not all(report.get(k) for k in ('completed', 'restored', 'applications_healthy')):
                raise RuntimeError('Child did not verify completion and restoration')
            print(implementation, 'completed:', Path(row['reports'][0])/'report.html', flush=True)
        status['state'] = 'completed'
    except BaseException as error:
        status.update(state='failed', error=type(error).__name__+': '+str(error))
        raise
    finally:
        if child is not None and child.poll() is None:
            # Let the child's existing finally block restore resources first.
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
                status['recovery_required'] = 'Child cleanup timed out; use its retained state.json checkpoint'
        status['finished'] = time.time();save(directory/'series.json', status)
        if guard:
            guard.terminate();guard.wait(timeout=5)


if __name__ == '__main__':
    main()
