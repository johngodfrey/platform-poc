import http from 'k6/http';
import { sleep } from 'k6';
import execution from 'k6/execution';
import { Counter, Gauge, Trend } from 'k6/metrics';
const e=__ENV, impl=e.IMPLEMENTATION;
const warm=Number(e.WARMUP), baseline=Number(e.BASELINE), burst=Number(e.BURST), recovery=Number(e.RECOVERY);
const low=Number(e.BASELINE_RATE), high=Number(e.BURST_RATE), window=Number(e.WINDOW_SECONDS);
const burstStart=warm+baseline+1, burstEnd=burstStart+burst, total=burstEnd+1+recovery;
const latency=new Trend('autoscale_latency_ms');
const accepted=new Counter('autoscale_accepted'), failed=new Counter('autoscale_failures'), completed=new Counter('autoscale_completed');
const gauges={};
for (const name of ['phase','offered_rps','desired_capacity','observed_capacity','cpu_cores','target_cpu_cores','signal_valid','controller_state','latency_p95_ms','target_latency_ms','latency_signal_valid','trigger_latency']) gauges[name]=new Gauge('autoscale_'+name);
const thresholds={};
for(let i=0;i<=Math.ceil((total+15)/window);i++) {
 thresholds[`autoscale_latency_ms{window:${i}}`]=['p(95)<1000000000'];
 for(const name of ['accepted','failures','completed']) thresholds[`autoscale_${name}{window:${i}}`]=['count>=0'];
}
export const options={tags:{implementation:impl,testid:e.RUN_ID,trigger:e.TRIGGER||'cpu'},thresholds,scenarios:{
 orders:{executor:'ramping-arrival-rate',exec:'order',startRate:low,timeUnit:'1s',preAllocatedVUs:Number(e.VUS),maxVUs:Number(e.VUS),gracefulStop:'15s',stages:[{duration:`${warm+baseline}s`,target:low},{duration:'1s',target:high},{duration:`${burst}s`,target:high},{duration:'1s',target:low},{duration:`${recovery}s`,target:low}]},
 timeline:{executor:'constant-vus',exec:'timeline',vus:1,duration:`${total}s`,gracefulStop:'2s'}
}};
function elapsed(){return (Date.now()-execution.scenario.startTime)/1000;}
export function order(){
 const tags={window:String(Math.floor(elapsed()/window))};
 const body=impl==='java'?{orderLineItemsDtoList:[{skuCode:'iphone_13',price:200,quantity:1}]}:[{sku:'ENG-V8-500',price:8500,quantity:1}];
 const url=impl==='java'?'http://localhost:8081/api/order':'http://localhost:8080/orders';
 const r=http.post(url,JSON.stringify(body),{headers:{'Content-Type':'application/json'},timeout:'10s',tags:{kind:'order'}});
 const ok=r.status===201&&(impl==='java'?r.body.includes('Placed Successfully'):r.body.trim()==='201 Created');
 latency.add(r.timings.duration,tags);accepted.add(ok?1:0,tags);failed.add(ok?0:1,tags);completed.add(1,tags);
}
let announced=false;
export function timeline(){
 if(!announced){console.log(JSON.stringify({event:'autoscale_started',start_ms:execution.scenario.startTime}));announced=true;}
 const t=elapsed();gauges.phase.add(t<warm?0:t<burstStart?1:t<burstEnd?2:3);gauges.offered_rps.add(t>=burstStart&&t<burstEnd?high:low);
 const r=http.get(e.CONTROL_URL,{timeout:'2s',tags:{kind:'control'}});
 if(r.status===200){const data=r.json();for(const [key,value] of Object.entries(data))if(gauges[key]&&value!==null)gauges[key].add(value);}
 sleep(1);
}
