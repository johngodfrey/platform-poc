#!/usr/bin/env python3
"""Generate the provisioned dashboard and Kubernetes resources (no third-party modules)."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parent
NS = 'monitoring'
items = []
def resource(kind, name, **fields):
    obj = dict(apiVersion='v1', kind=kind, metadata=dict(name=name, namespace=NS), **fields)
    items.append(obj)
    return obj
resource('Namespace', NS)['metadata'].pop('namespace')
resource('ServiceAccount', 'footprint-prometheus')
role = resource('ClusterRole', 'platform-poc-footprint', rules=[dict(apiGroups=[''], resources=['nodes', 'nodes/proxy'], verbs=['get', 'list', 'watch'])])
role['apiVersion'] = 'rbac.authorization.k8s.io/v1'; role['metadata'].pop('namespace')
binding = resource('ClusterRoleBinding', 'platform-poc-footprint', roleRef=dict(apiGroup='rbac.authorization.k8s.io', kind='ClusterRole', name='platform-poc-footprint'), subjects=[dict(kind='ServiceAccount', name='footprint-prometheus', namespace=NS)])
binding['apiVersion'] = 'rbac.authorization.k8s.io/v1'; binding['metadata'].pop('namespace')
config = {
 'global': {'scrape_interval': '5s', 'evaluation_interval': '5s'},
 'rule_files': ['/etc/prometheus/rules.json'],
 'scrape_configs': [{
  'job_name': 'kubelet-cadvisor', 'scheme': 'https',
  'authorization': {'credentials_file': '/var/run/secrets/kubernetes.io/serviceaccount/token'},
  'tls_config': {'ca_file': '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'},
  'kubernetes_sd_configs': [{'role': 'node'}],
  'relabel_configs': [
   {'source_labels': ['__meta_kubernetes_node_name'], 'target_label': 'node'},
   {'target_label': '__address__', 'replacement': 'kubernetes.default.svc:443'},
   {'source_labels': ['__meta_kubernetes_node_name'], 'target_label': '__metrics_path__', 'replacement': '/api/v1/nodes/${1}/proxy/metrics/cadvisor'},
  ],
  'metric_relabel_configs': [
   {'source_labels': ['__name__'], 'regex': 'container_memory_working_set_bytes|container_cpu_usage_seconds_total', 'action': 'keep'},
   {'source_labels': ['namespace'], 'regex': 'java|wasm|monitoring', 'action': 'keep'},
   {'source_labels': ['container'], 'regex': '|POD', 'action': 'drop'},
  ],
 }],
}
rules = []
for implementation in ['java', 'wasm']:
    selector = f'namespace="{implementation}",job="kubelet-cadvisor"'
    app = selector + (',container="wasmcloud-host"' if implementation == 'wasm' else ',container=~".*service.*"')
    for scope, labels in [('app', app), ('namespace', selector)]:
        for suffix, expr in [('memory_bytes', f'sum(container_memory_working_set_bytes{{{labels}}})'), ('cpu_cores', f'sum(rate(container_cpu_usage_seconds_total{{{labels}}}[30s]))')]:
            rules.append({'record': f'poc_{scope}_{suffix}', 'expr': expr, 'labels': {'implementation': implementation}})
resource('ConfigMap', 'footprint-prometheus', data={'prometheus.json': json.dumps(config, indent=2), 'rules.json': json.dumps({'groups': [{'name': 'footprint', 'rules': rules}]}, indent=2)})

panels = []
def panel(title, queries, unit, x, y, w=12, h=8, kind='timeseries', description=''):
    targets = [{'refId': chr(65+i), 'expr': q, 'legendFormat': legend, 'datasource': {'type': 'prometheus', 'uid': 'footprint-prometheus'}} for i,(q,legend) in enumerate(queries)]
    p = {'id': len(panels)+1, 'title': title, 'type': kind, 'gridPos': {'x': x, 'y': y, 'w': w, 'h': h}, 'datasource': {'type': 'prometheus', 'uid': 'footprint-prometheus'}, 'targets': targets, 'description': description, 'fieldConfig': {'defaults': {'unit': unit, 'min': 0, 'color': {'mode': 'palette-classic'}}, 'overrides': []}, 'options': {'legend': {'displayMode': 'table', 'placement': 'bottom', 'calcs': ['lastNotNull', 'max']}, 'tooltip': {'mode': 'multi'}}}
    if kind == 'stat': p['options'] = {'reduceOptions': {'calcs': ['lastNotNull'], 'values': False}, 'colorMode': 'value', 'graphMode': 'area'}
    p['fieldConfig']['overrides'] = [
        {'matcher': {'id': 'byRegexp', 'options': '^java.*'}, 'properties': [{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': '#5794F2'}}]},
        {'matcher': {'id': 'byRegexp', 'options': '^wasm.*'}, 'properties': [{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': '#73BF69'}}]},
    ]
    panels.append(p)
filt = 'testid=~"$run",implementation=~"$implementation"'
# k6 labels each scenario separately. Sum counters before presenting the run totals.
accepted = f'sum by (implementation,testid) (rate(k6_demo_orders_accepted_total{{{filt}}}[20s]))'
failures = f'sum by (implementation,testid) (rate(k6_demo_order_failures_total{{{filt}}}[20s]))'
panel('Application memory — current', [('poc_app_memory_bytes', '{{implementation}}')], 'bytes', 0, 0, 8, 4, 'stat')
panel('Application CPU — current', [('poc_app_cpu_cores', '{{implementation}}')], 'cores', 8, 0, 8, 4, 'stat')
panel('Accepted orders / second', [(accepted, '{{implementation}} {{testid}}')], 'ops', 16, 0, 8, 4, 'stat')
panel('Application memory', [('poc_app_memory_bytes', '{{implementation}}')], 'bytes', 0, 4, description='Java application containers; Wasm host container includes its child provider processes. Working set, not requests or limits.')
panel('Application CPU', [('poc_app_cpu_cores', '{{implementation}}')], 'cores', 12, 4, description='30-second CPU rate; 1 core = 1,000 millicores. Monitoring overhead is separate.')
panel('Kubernetes namespace memory', [('poc_namespace_memory_bytes', '{{implementation}}')], 'bytes', 0, 12, description='All Java/Wasm containers, including Wasm sidecar, NATS, operator and wadm. EXCLUDES host Java NATS and shared Postgres/Redis; not a complete deployment total.')
panel('Kubernetes namespace CPU', [('poc_namespace_cpu_cores', '{{implementation}}')], 'cores', 12, 12)
panel('Delivered versus offered orders / second', [(accepted, '{{implementation}} accepted'), (f'max by (implementation,testid) (k6_demo_target_rps{{{filt}}})', '{{implementation}} offered')], 'ops', 0, 20, description='Accepted rate uses a 20-second window. Only footprint demo runs emit these custom metrics.')
panel('Order latency (cumulative run statistics)', [(f'k6_http_req_duration_p95{{{filt},scenario="orders"}}', '{{implementation}} p95'), (f'k6_http_req_duration_avg{{{filt},scenario="orders"}}', '{{implementation}} average')], 's', 12, 20, description='k6 trend gauges are statistics accumulated during each run, not percentiles over the visible dashboard time range. Do not average percentiles across runs.')
panel('Failures and dropped orders / second', [(failures, '{{implementation}} failed'), (f'sum by (implementation,testid) (rate(k6_dropped_iterations_total{{{filt}}}[20s]))', '{{implementation}} dropped')], 'ops', 0, 28)
panel('Demo phase: 0 idle / 1 load / 2 recovery', [(f'max by (implementation,testid) (k6_demo_phase{{{filt}}})', '{{implementation}} {{testid}}')], 'short', 12, 28)
panel('Application CPU-seconds / 1,000 accepted orders', [(f'poc_app_cpu_cores / on (implementation) (sum by (implementation) ({accepted}) > 0) * 1000', '{{implementation}}')], 's', 0, 36, description='Live ratio of application CPU rate to accepted order rate; meaningful during steady load, not idle or transitions.')
panel('Monitoring footprint (excluded from application totals)', [('sum(container_memory_working_set_bytes{namespace="monitoring",job="kubelet-cadvisor"})', 'monitoring memory')], 'bytes', 12, 36)
panels.append({'id': 99, 'title': 'How to present this demo', 'type': 'text', 'gridPos': {'x': 0, 'y': 44, 'w': 24, 'h': 5}, 'options': {'mode': 'markdown', 'content': 'Run Java and Wasm sequentially: **30s idle → 120s at 100 orders/s → 30s recovery**. Both use 250 order workers plus one marker worker. Charts refresh every 5s; CPU is smoothed over 30s. Resource charts show both live deployments, while the implementation/run selectors filter load-test results. Memory is working set. **Shared host databases and Java’s host NATS are excluded.** Monitoring runs in its own namespace. No cold-start claims: start warmed. k3d CPU and memory do not include all VM/macOS overhead. Run IDs separate repeated tests; missing test data means no run has been received, not zero load.'}})
dashboard = {'uid': 'java-wasm-footprint', 'title': 'Java vs Wasm — live footprint', 'schemaVersion': 39, 'version': 1, 'refresh': '5s', 'time': {'from': 'now-15m', 'to': 'now'}, 'tags': ['platform-poc', 'footprint'], 'panels': panels, 'templating': {'list': [
 {'name': 'implementation', 'type': 'custom', 'query': 'java,wasm', 'includeAll': True, 'allValue': '.*', 'multi': True, 'current': {'text': 'All', 'value': '$__all'}},
 {'name': 'run', 'label': 'Run', 'type': 'query', 'datasource': {'type': 'prometheus', 'uid': 'footprint-prometheus'}, 'query': {'query': 'label_values(k6_demo_phase, testid)', 'refId': 'runs'}, 'refresh': 2, 'includeAll': True, 'allValue': '.*', 'multi': True, 'current': {'text': 'All', 'value': '$__all'}},
]}}
(ROOT/'dashboard.json').write_text(json.dumps(dashboard, indent=2)+'\n')
resource('ConfigMap', 'footprint-grafana', data={
 'datasource.yaml': json.dumps({'apiVersion': 1, 'datasources': [{'name': 'Footprint Prometheus', 'uid': 'footprint-prometheus', 'type': 'prometheus', 'access': 'proxy', 'url': 'http://footprint-prometheus:9090/prometheus', 'isDefault': True, 'jsonData': {'timeInterval': '5s'}}]}),
 'provider.yaml': json.dumps({'apiVersion': 1, 'providers': [{'name': 'footprint', 'type': 'file', 'disableDeletion': False, 'updateIntervalSeconds': 10, 'options': {'path': '/var/lib/grafana/dashboards'}}]}),
 'dashboard.json': json.dumps(dashboard),
})
for name, size in [('footprint-prometheus', '2Gi'), ('footprint-grafana', '1Gi')]:
 resource('PersistentVolumeClaim', name, spec={'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': size}}})
def deployment(name, container, volumes, service_account=None, uid=65534):
 spec={'securityContext': {'runAsUser': uid, 'runAsGroup': uid, 'fsGroup': uid}, 'containers': [container], 'volumes': volumes}
 if service_account: spec['serviceAccountName']=service_account
 dep=resource('Deployment', name, spec={'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': name}}, 'template': {'metadata': {'labels': {'app': name}}, 'spec': spec}})
 dep['apiVersion']='apps/v1'
def probe(path,port): return {'httpGet': {'path': path, 'port': port}, 'initialDelaySeconds': 5, 'periodSeconds': 5, 'timeoutSeconds': 5, 'failureThreshold': 3}
deployment('footprint-prometheus', {
 'name': 'prometheus', 'image': 'prom/prometheus:v3.15.1',
 'args': ['--config.file=/etc/prometheus/prometheus.json','--storage.tsdb.path=/prometheus','--storage.tsdb.retention.time=7d','--storage.tsdb.retention.size=1500MB','--web.enable-remote-write-receiver','--web.external-url=/prometheus/','--web.route-prefix=/prometheus'],
 'ports': [{'containerPort': 9090}], 'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'memory': '768Mi'}},
 'readinessProbe': probe('/prometheus/-/ready',9090),
 'volumeMounts': [{'name': 'config','mountPath':'/etc/prometheus','readOnly':True},{'name':'data','mountPath':'/prometheus'}],
}, [{'name':'config','configMap':{'name':'footprint-prometheus'}},{'name':'data','persistentVolumeClaim':{'claimName':'footprint-prometheus'}}], 'footprint-prometheus')
env={'GF_SERVER_ROOT_URL':'http://localhost:8080/grafana/','GF_SERVER_SERVE_FROM_SUB_PATH':'true','GF_AUTH_ANONYMOUS_ENABLED':'true','GF_AUTH_ANONYMOUS_ORG_ROLE':'Viewer','GF_USERS_ALLOW_SIGN_UP':'false','GF_ANALYTICS_REPORTING_ENABLED':'false','GF_ANALYTICS_CHECK_FOR_UPDATES':'false','GF_SECURITY_ADMIN_PASSWORD__FILE':'/etc/grafana/admin/password'}
env.update({'GF_DATABASE_WAL':'true', 'GF_DATABASE_MAX_OPEN_CONN':'1',
            'GOMAXPROCS':'2', 'GOMEMLIMIT':'768MiB'})
deployment('footprint-grafana', {
 'name':'grafana','image':'grafana/grafana:13.2.4','env':[{'name':k,'value':v} for k,v in env.items()], 'ports':[{'containerPort':3000}],
 'resources':{'requests':{'cpu':'200m','memory':'256Mi'},'limits':{'cpu':'1','memory':'1Gi'}},'readinessProbe':probe('/api/health',3000),
 'volumeMounts':[{'name':'data','mountPath':'/var/lib/grafana'},{'name':'config','mountPath':'/etc/grafana/provisioning/datasources/datasource.yaml','subPath':'datasource.yaml','readOnly':True},{'name':'config','mountPath':'/etc/grafana/provisioning/dashboards/provider.yaml','subPath':'provider.yaml','readOnly':True},{'name':'config','mountPath':'/var/lib/grafana/dashboards/dashboard.json','subPath':'dashboard.json','readOnly':True},{'name':'admin','mountPath':'/etc/grafana/admin','readOnly':True}],
},[{'name':'config','configMap':{'name':'footprint-grafana'}},{'name':'data','persistentVolumeClaim':{'claimName':'footprint-grafana'}},{'name':'admin','secret':{'secretName':'footprint-grafana-admin','defaultMode':288}}],uid=472)
for name, port in [('footprint-prometheus',9090),('footprint-grafana',3000)]:
 resource('Service',name,spec={'selector':{'app':name},'ports':[{'port':port,'targetPort':port,'name':'http'}]})
ing=resource('Ingress','footprint',spec={'ingressClassName':'traefik','rules':[{'http':{'paths':[{'path':path,'pathType':'Prefix','backend':{'service':{'name':svc,'port':{'number':port}}}} for path,svc,port in [('/grafana','footprint-grafana',3000),('/prometheus','footprint-prometheus',9090)]]}}]})
ing['apiVersion']='networking.k8s.io/v1'
(ROOT/'resources.json').write_text(json.dumps({'apiVersion':'v1','kind':'List','items':items},indent=2)+'\n')
print('Generated dashboard.json and resources.json')
