"""Build the memory-density view using the shared load-test panel definitions."""
from copy import deepcopy

CPU_QUERY = 'max by (node) (sum by (node) (rate(container_cpu_usage_seconds_total{job="density-node",id="/"}[30s])) / on (node) max by (node) (machine_cpu_cores{job="density-node"}))'
CPU_QUERY = '('+CPU_QUERY+') and on (node) (min by (node) (min_over_time(up{job="density-node"}[30s])) == 1)'


def build(template):
    dashboard = deepcopy(template)
    dashboard.update(uid='java-wasm-memory-density', title='Java vs Wasm — memory density', tags=['platform-poc','memory-density'], time={'from':'now-1h','to':'now'})
    dashboard['templating']['list'][1]['query']['query'] = 'label_values(k6_demo_phase{testid=~"density-.*"}, testid)'
    for panel in dashboard['panels']:
        for target in panel.get('targets',[]):
            target['expr'] = target['expr'].replace('testid!~"budget-.*|density-.*|capacity-.*|scaleout-.*"','testid=~"density-.*"')
    # Reuse only the relevant application and load panels; namespace totals have
    # a different scope from the memory allocations being swept.
    dashboard['panels'] = [p for p in dashboard['panels'] if p['id'] not in [6,7,13,99]]
    for p in dashboard['panels']:
        if p['gridPos']['y'] >= 20: p['gridPos']['y'] -= 8
    seed = deepcopy(dashboard['panels'][0])
    filt = 'testid=~"$run",implementation=~"$implementation",testid=~"density-.*"'
    def add(id,title,expr,legend,unit,x,y,description):
        panel = deepcopy(seed)
        panel.update(id=id,title=title,description=description,type='timeseries',gridPos=dict(x=x,y=y,w=12,h=8))
        panel['targets'] = [dict(panel['targets'][0],expr=expr,legendFormat=legend)]
        panel['fieldConfig']['defaults']['unit'] = unit
        panel['options'] = {'legend':{'displayMode':'table','placement':'bottom','calcs':['lastNotNull','max']},'tooltip':{'mode':'multi'}}
        dashboard['panels'].append(panel)
    add(101,'Configured total application memory limit',f'max by (implementation,testid) (k6_density_memory_limit_bytes{{{filt}}})','{{implementation}} {{testid}}','bytes',0,36,'Java divides this limit equally across four application containers; Wasm assigns it to the host. CPU limits are removed.')
    add(102,'Projected slots at current cap — unvalidated',f'max by (implementation,testid) (k6_density_projected_slots{{{filt}}})','{{implementation}} {{testid}}','short',12,36,'floor(application-only RAM pool / current allocation). This is a hypothesis, not a count of deployed stacks or a pass verdict. See report.html for allocations that passed every repeat.')
    add(103,'Node CPU utilization — headroom check',CPU_QUERY,'{{node}}','percentunit',0,44,'30-second sampled node CPU / node core count. The report marks stages inconclusive if metrics are missing or any node reaches the configured ceiling. k3d nodes share one VM: this does not measure macOS CPU pressure or prove physical CPU isolation.')
    add(104,'Maximum allowed node CPU utilization',f'max by (implementation,testid) (k6_density_max_node_cpu_fraction{{{filt}}})','{{implementation}} {{testid}}','percentunit',12,44,'Default ceiling is 60%. CPU and memory resource charts show live deployments independent of Run selection.')
    dashboard['panels'].append(dict(id=199,title='Memory-density experiment',type='text',gridPos=dict(x=0,y=52,w=24,h=6),options=dict(mode='markdown',content='Run **`python3 monitoring/density/run.py run both`**. Fixed order rate, descending memory limits, no application CPU limits, warmup at every limit and repeated measurements. **Projected slots are not proven concurrent capacity.** An allocation passes only when all repeats meet latency/reliability gates with sampled node CPU headroom. Missing CPU telemetry or runtime interruptions are inconclusive. RAM pool excludes infrastructure and host overhead. Java uses equal per-service allocations; Wasm shares its host allocation. Stop other tests, keep the machine awake, and consult **report.html** for the smallest passing tested allocation and any inconclusive outcomes.')))
    return dashboard
