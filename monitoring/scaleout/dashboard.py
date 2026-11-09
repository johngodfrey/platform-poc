from copy import deepcopy


def build(template):
    d=deepcopy(template)
    d.update(uid='java-wasm-scaleout',title='Java vs Wasm — scale-out',tags=['platform-poc','scaleout'],time={'from':'now-30m','to':'now'})
    d['templating']['list'][1]['query']['query']='label_values(k6_scaleout_phase{testid=~"scaleout-.*"}, testid)'
    seed=deepcopy(template['panels'][3])
    d['panels']=[]
    filt='testid=~"$run",implementation=~"$implementation",testid=~"scaleout-.*"'
    def add(title,queries,unit,x,y,description=''):
        p=deepcopy(seed);p.update(id=len(d['panels'])+1,title=title,gridPos=dict(x=x,y=y,w=12,h=8),description=description)
        p['fieldConfig']['defaults']['unit']=unit
        p['targets']=[dict(refId=chr(65+i),expr=q,legendFormat=legend,datasource=p['datasource']) for i,(q,legend) in enumerate(queries)]
        d['panels'].append(p)
    add('Application memory',[('poc_app_memory_bytes','{{implementation}}')],'bytes',0,0,'Live deployment working sets; aggregate telemetry has shown inconsistencies after restarts. Run selection does not filter resource charts.')
    add('Application CPU',[('poc_app_cpu_cores','{{implementation}}')],'cores',12,0,'30-second CPU rate, not CPU limits.')
    add('Offered and accepted orders/s',[(f'max by (implementation,testid) (k6_scaleout_offered_rps{{{filt}}})','{{implementation}} offered'),(f'sum by (implementation,testid) (rate(k6_scaleout_accepted_total{{{filt}}}[15s]))','{{implementation}} accepted')],'ops',0,8)
    add('Order p95 — separate 15-second request-start cohorts',[(f'k6_scaleout_latency_ms_p95{{{filt}}}','{{implementation}} window {{window}}')],'ms',12,8,'Each line accumulates only within its tagged request-start cohort, not the entire run. The report requires two full passing windows after the actual scale command; no retries.')
    add('Java ready order pods with successful direct orders',[(f'k6_scaleout_observed_capacity{{{filt},implementation="java"}}','java observed'),(f'k6_scaleout_desired_capacity{{{filt},implementation="java"}}','java desired')],'short',0,16)
    add('Wasm capacity — hosts or concurrency (see mode)',[(f'k6_scaleout_observed_capacity{{{filt},implementation="wasm"}}','wasm {{wasm_mode}} observed'),(f'k6_scaleout_desired_capacity{{{filt},implementation="wasm"}}','wasm {{wasm_mode}} desired')],'short',12,16,'Host mode: verified routable runtime hosts (1→2). Concurrency mode: orders max_instances (10→20). Do not compare the units across modes.')
    add('Failures and dropped work/s',[(f'sum by (implementation,testid) (rate(k6_scaleout_failures_total{{{filt}}}[15s]))','{{implementation}} failures'),(f'sum by (implementation,testid) (rate(k6_dropped_iterations_total{{{filt}}}[15s]))','{{implementation}} dropped')],'ops',0,24)
    add('Phase: 0 warm / 1 baseline / 2 burst / 3 scale period / 4 recovery',[(f'k6_scaleout_phase{{{filt}}}','{{implementation}}')],'short',12,24,'Phase 3 begins at planned scale time. The actual command-accepted chart and report record observed timing.')
    add('Scale command accepted',[(f'k6_scaleout_command_accepted{{{filt}}}','{{implementation}}')],'short',0,32)
    add('Completed orders/s',[(f'sum by (implementation,testid) (rate(k6_scaleout_completed_total{{{filt}}}[15s]))','{{implementation}} completions')],'ops',12,32)
    add('Capacity observation available',[(f'k6_scaleout_observation_valid{{{filt}}}','{{implementation}}')],'short',0,40,'Zero means capacity polling failed; the last capacity gauge can be stale. Readiness timing is recorded only after a successful observation.')
    add('Wasm host pods and aggregate orders allowance',[(f'k6_scaleout_host_ready_pods{{{filt},implementation="wasm"}}','ready host pods'),(f'k6_scaleout_orders_concurrency{{{filt},implementation="wasm"}}','orders concurrency allowance')],'short',12,40,'New host mode preserves the patched runtime and timeout settings. New pods join HTTP routing only after runtime/component checks and a direct successful order.')
    add('New pod readiness — common protocol',[(f'k6_scaleout_new_pod_ready_seconds{{{filt}}}','{{implementation}} pod Ready'),(f'k6_scaleout_new_pod_order_seconds{{{filt}}}','{{implementation}} direct order accepted')],'s',0,48,'Independent 2-second polling and identical HTTP 201/direct-order checks with a 10-second timeout. Timings start when the scale request begins. Native Kubernetes Ready probes differ; functional order acceptance is the common application gate. Wasm full-host verification remains separate.')
    d['panels'].append(dict(id=99,title='Interpretation',type='text',gridPos=dict(x=0,y=56,w=24,h=7),options=dict(mode='markdown',content='Run **`python3 monitoring/scaleout/run.py run both --wasm-mode host`** to start a real additional Wasm host pod with providers and serving components. Java adds only an order-service pod, so these scale different scopes. Use **`--wasm-mode concurrency`** for the previous existing-host test. Images may be cached. Readiness requires runtime inventory, matching images, a direct successful order against the new pod and a ready persistent HTTP endpoint. The report separates common pod Ready/direct-order timings, command acceptance, full capacity and traffic recovery. Use repeat.py for five alternating pairs and median/range charts. Recovery requires two full passing request-start windows after readiness. Original capacity/version is restored; keep the runner terminal open.')))
    return d
