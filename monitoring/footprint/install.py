#!/usr/bin/env python3
"""Install only the local footprint monitoring resources."""
import base64
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
K = ['kubectl', '--context', 'k3d-platform-poc']

def run(*args, **kwargs):
    return subprocess.run(*args, check=True, **kwargs)

run([sys.executable, str(ROOT / 'generate.py')])
namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'monitoring'}}
run(K + ['apply', '-f', '-'], input=json.dumps(namespace), text=True)
exists = subprocess.run(K + ['-n', 'monitoring', 'get', 'secret', 'footprint-grafana-admin'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if exists.returncode:
    secret = {'apiVersion': 'v1', 'kind': 'Secret',
              'metadata': {'name': 'footprint-grafana-admin', 'namespace': 'monitoring'},
              'data': {'password': base64.b64encode(secrets.token_urlsafe(32).encode()).decode()}}
    run(K + ['create', '-f', '-'], input=json.dumps(secret), text=True)
run(K + ['apply', '-f', str(ROOT / 'resources.json')])
for name in ['footprint-prometheus', 'footprint-grafana']:
    # Provisioned subPath mounts need a restart when their ConfigMap changes.
    run(K + ['-n', 'monitoring', 'rollout', 'restart', 'deployment/' + name])
    run(K + ['-n', 'monitoring', 'rollout', 'status', 'deployment/' + name, '--timeout=240s'])
print('\nDashboard: http://localhost:8080/grafana/d/java-wasm-footprint')
print('Read-only dashboard access requires no login. Prometheus: http://localhost:8080/prometheus/')
