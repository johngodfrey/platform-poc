from copy import deepcopy


def build(template):
    dashboard=deepcopy(template)
    dashboard.update(uid='java-wasm-autoscale',title='Java vs Wasm — automatic scaling',tags=['platform-poc','autoscale'],time={'from':'now-30m','to':'now'})
    dashboard['templating']['list'][1]['query']['query']='label_values(k6_autoscale_phase{testid=~"autoscale-.*"}, testid)'
    seed=deepcopy(template['panels'][3]);dashboard['panels']=[]
    filt='testid=~"$run",implementation=~"$implementation",testid=~"autoscale-.*"'
    def panel(title,queries,unit,description=''):
        n=len(dashboard['panels']);item=deepcopy(seed);item.update(id=n+1,title=title,gridPos=dict(x=(n%2)*12,y=(n//2)*8,w=12,h=8),description=description)
        item['fieldConfig']['defaults']['unit']=unit
        item['targets']=[dict(refId=chr(65+i),expr=query,legendFormat=legend,datasource=item['datasource']) for i,(query,legend) in enumerate(queries)]
        dashboard['panels'].append(item)
    panel('CPU signal and target per capacity unit',[(f'k6_autoscale_cpu_cores{{{filt}}}','{{implementation}} total CPU'),(f'k6_autoscale_target_cpu_cores{{{filt}}}','{{implementation}} target/unit')],'cores','60-second CPU rates. Java order-service CPU versus Wasm runtime CPU; targets are independently calibrated. Divide total CPU by serving capacity for downscale decisions.')
    panel('Desired and verified serving capacity',[(f'k6_autoscale_desired_capacity{{{filt}}}','{{implementation}} desired'),(f'k6_autoscale_observed_capacity{{{filt}}}','{{implementation}} serving')],'short','Java order pods versus complete Wasm hosts; distinct scaling scopes.')
    panel('Offered and accepted orders/s',[(f'k6_autoscale_offered_rps{{{filt}}}','{{implementation}} offered'),(f'sum by (implementation,testid)(rate(k6_autoscale_accepted_total{{{filt}}}[15s]))','{{implementation}} accepted')],'ops')
    panel('Order p95 — request-start cohorts',[(f'k6_autoscale_latency_ms_p95{{{filt}}}','{{implementation}} window {{window}}')],'ms','Independent 15-second request-start cohorts; final gates are calculated in report.html.')
    panel('CPU signal validity',[(f'k6_autoscale_signal_valid{{{filt}}}','{{implementation}} fresh signal')],'short','Zero disables CPU decisions and resets the downscale stabilization timer.')
    panel('Controller: 0 waiting, 1 up requested, 2 down requested',[(f'k6_autoscale_controller_state{{{filt}}}','{{implementation}} controller')],'short')
    panel('Application CPU',[('poc_app_cpu_cores','{{implementation}}')],'cores','Live resource series are not filtered by run.')
    panel('Application memory',[('poc_app_memory_bytes','{{implementation}}')],'bytes')
    panel('Failures and dropped work/s',[(f'sum by (implementation,testid)(rate(k6_autoscale_failures_total{{{filt}}}[15s]))','{{implementation}} failed'),(f'sum by (implementation,testid)(rate(k6_dropped_iterations_total{{{filt}}}[15s]))','{{implementation}} dropped')],'ops')
    panel('Phase: 0 warm, 1 baseline, 2 burst, 3 recovery',[(f'k6_autoscale_phase{{{filt}}}','{{implementation}} phase')],'short','Phases control load only; observed CPU or latency drives scaling actions.')
    panel('Settled latency signal and scale-out threshold',[(f'k6_autoscale_latency_p95_ms{{{filt}}}','{{implementation}} settled cohort p95'),(f'k6_autoscale_target_latency_ms{{{filt}}}','{{implementation}} threshold')],'ms','Latency mode scales out after two consecutive settled 15-second cohorts above 500 ms. Waits 20 seconds after cohort end for the 10-second HTTP limit and metric export. CPU mode ignores this signal.')
    panel('Latency signal validity and selected policy',[(f'k6_autoscale_latency_signal_valid{{{filt}}}','{{implementation}} valid latency'),(f'k6_autoscale_trigger_latency{{{filt}}}','{{implementation}} latency mode (1=yes)')],'short')
    dashboard['panels'].append(dict(id=99,title='Interpretation',type='text',gridPos=dict(x=0,y=48,w=24,h=7),options=dict(mode='markdown',content='Run `python3 monitoring/autoscale/run.py run` for CPU scale-out; add `--trigger latency` for two consecutive settled p95 cohorts above 500 ms. This is a shared benchmark controller, **not native Kubernetes HPA**. Both modes keep CPU-based scale-in: 60 seconds below 70% average target. CPU target is 1.25× warmup median (minimum 50m), capped at two units and one cycle. Java adds an order pod; Wasm adds a whole serving host. CPU scopes differ. Latency decisions include cohort/settling/export delay. The report separates trigger, direct new-pod order, serving capacity, traffic recovery and downscale. Forced final cleanup is not automatic scaling.')))
    return dashboard
