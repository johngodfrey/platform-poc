import http from 'k6/http';
import { sleep } from 'k6';
import execution from 'k6/execution';
import { Counter, Gauge, Trend } from 'k6/metrics';
const env = __ENV;
const impl = env.IMPLEMENTATION;
const warm = Number(env.WARMUP), baseline = Number(env.BASELINE), before = Number(env.BEFORE_SCALE);
const after = Number(env.AFTER_SCALE), recovery = Number(env.RECOVERY);
const low = Number(env.BASELINE_RATE), high = Number(env.BURST_RATE), window = Number(env.WINDOW_SECONDS);
const burstStart = warm + baseline + 1, scaleAt = burstStart + before;
const burstEnd = burstStart + before + after, total = burstEnd + 1 + recovery;
const latency = new Trend('scaleout_latency_ms');
const accepted = new Counter('scaleout_accepted');
const failures = new Counter('scaleout_failures');
const completed = new Counter('scaleout_completed');
const phase = new Gauge('scaleout_phase');
const offered = new Gauge('scaleout_offered_rps');
const observed = new Gauge('scaleout_observed_capacity');
const desired = new Gauge('scaleout_desired_capacity');
const command = new Gauge('scaleout_command_accepted');
const valid = new Gauge('scaleout_observation_valid');
const pods = new Gauge('scaleout_host_ready_pods');
const concurrency = new Gauge('scaleout_orders_concurrency');
const podReady = new Gauge('scaleout_new_pod_ready_seconds');
const podOrder = new Gauge('scaleout_new_pod_order_seconds');
const thresholds = {};
for (let i=0; i<=Math.ceil((total+15)/window); i++) {
  thresholds[`scaleout_latency_ms{window:${i}}`] = ['p(95)<1000000000'];
  for (const m of ['scaleout_accepted','scaleout_failures','scaleout_completed']) thresholds[`${m}{window:${i}}`] = ['count>=0'];
}
export const options = {
 tags: {implementation:impl, testid:env.RUN_ID,wasm_mode:env.WASM_MODE || 'concurrency'}, thresholds,
 scenarios: {
  orders: {executor:'ramping-arrival-rate',exec:'order',startRate:low,timeUnit:'1s',
   preAllocatedVUs:Number(env.VUS),maxVUs:Number(env.VUS),gracefulStop:'15s',
   stages:[{duration:`${warm+baseline}s`,target:low},{duration:'1s',target:high},
    {duration:`${before+after}s`,target:high},{duration:'1s',target:low},{duration:`${recovery}s`,target:low}]},
  timeline: {executor:'constant-vus',exec:'timeline',vus:1,duration:`${total}s`,gracefulStop:'2s'}
 }
};
function elapsed() { return (Date.now()-execution.scenario.startTime)/1000; }
export function order() {
 const tags = {window:String(Math.floor(elapsed()/window))};
 const payload = impl==='java' ? {orderLineItemsDtoList:[{skuCode:'iphone_13',price:200,quantity:1}]} : [{sku:'ENG-V8-500',price:8500,quantity:1}];
 const base = impl==='java' ? 'http://localhost:8081/api/order' : 'http://localhost:8080/orders';
 const r = http.post(base,JSON.stringify(payload),{headers:{'Content-Type':'application/json'},timeout:'10s',tags:{kind:'order'}});
 const ok = r.status===201 && (impl!=='java' || r.body.includes('Placed Successfully'));
 latency.add(r.timings.duration,tags);accepted.add(ok?1:0,tags);failures.add(ok?0:1,tags);completed.add(1,tags);
}
let announced = false;
export function timeline() {
 if (!announced) { console.log(JSON.stringify({event:'scaleout_started',start_ms:execution.scenario.startTime})); announced=true; }
 const t=elapsed();
 phase.add(t<warm?0:t<burstStart?1:t<scaleAt?2:t<burstEnd?3:4);
 offered.add(t>=burstStart && t<burstEnd?high:low);
 const r=http.get(env.CONTROL_URL,{timeout:'2s',tags:{kind:'control'}});
 if(r.status===200) {
  const s=r.json();desired.add(s.desired);valid.add(s.observed===null?0:1);
  if(s.observed!==null)observed.add(s.observed);command.add(s.accepted?1:0);
  if(s.ready_pods!=null)pods.add(s.ready_pods);
  if(s.orders_concurrency!=null)concurrency.add(s.orders_concurrency);
  if(s.new_pod_ready_seconds!=null)podReady.add(s.new_pod_ready_seconds);
  if(s.new_pod_order_accepted_seconds!=null)podOrder.add(s.new_pod_order_accepted_seconds);
 }
 sleep(1);
}
