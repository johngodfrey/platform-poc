import http from 'k6/http';
import { check } from 'k6';
export const options={thresholds:{checks:['rate==1'],http_req_failed:['rate==0'],dropped_iterations:['count==0']},scenarios:{orders:{executor:'constant-arrival-rate',rate:100,timeUnit:'1s',duration:'30s',preAllocatedVUs:250,maxVUs:250,gracefulStop:'12s'}}};
export default function(){
 const wasm=__ENV.IMPLEMENTATION==='wasm';
 const payload=wasm?[{sku:'ENG-V8-500',price:8500,quantity:1}]:{orderLineItemsDtoList:[{skuCode:'iphone_13',price:200,quantity:1}]};
 const response=http.post(wasm?'http://localhost:8080/orders':'http://localhost:8081/api/order',JSON.stringify(payload),{headers:{'Content-Type':'application/json'},timeout:'10s'});
 check(response,{'order accepted':r=>r.status===201&&(wasm?r.body.trim()==='201 Created':r.body.includes('Placed Successfully'))});
}
