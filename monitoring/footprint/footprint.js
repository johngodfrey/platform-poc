import { check, sleep } from 'k6';
import http from 'k6/http';
import execution from 'k6/execution';
import { Counter, Gauge } from 'k6/metrics';

const implementation = __ENV.IMPLEMENTATION;
if (!['java', 'wasm'].includes(implementation)) throw new Error('IMPLEMENTATION must be java or wasm');
const idle = Number(__ENV.IDLE_SECONDS || 30);
const load = Number(__ENV.LOAD_SECONDS || 120);
const recovery = Number(__ENV.RECOVERY_SECONDS || 30);
const rate = Number(__ENV.ORDER_RATE || 100);
for (const value of [idle, load, recovery, rate]) {
  if (!Number.isInteger(value) || value < 1) throw new Error('Durations and ORDER_RATE must be positive integers');
}
const base = (__ENV.BASE_URL || (implementation === 'java' ? 'http://localhost:8081' : 'http://localhost:8080')).replace(/\/$/, '');
const accepted = new Counter('demo_orders_accepted');
const failed = new Counter('demo_order_failures');
const phase = new Gauge('demo_phase');
const target = new Gauge('demo_target_rps');
export const options = {
  tags: { implementation, testid: __ENV.RUN_ID || 'manual' },
  thresholds: { demo_order_failures: ['count==0'], dropped_iterations: ['count==0'] },
  scenarios: {
    orders: { executor: 'constant-arrival-rate', exec: 'order', rate, timeUnit: '1s',
      startTime: `${idle}s`, duration: `${load}s`, preAllocatedVUs: 250, maxVUs: 250, gracefulStop: '30s' },
    // One additional worker emits phase markers; it sends no HTTP requests.
    timeline: { executor: 'constant-vus', exec: 'timeline', vus: 1,
      duration: `${idle + load + recovery}s`, gracefulStop: '2s' },
  },
};
export function timeline() {
  const elapsed = (Date.now() - execution.scenario.startTime) / 1000;
  const current = elapsed < idle ? 0 : elapsed < idle + load ? 1 : 2;
  phase.add(current);
  target.add(current === 1 ? rate : 0);
  accepted.add(0);
  failed.add(0);
  sleep(1);
}
export function order() {
  const payload = implementation === 'java'
    ? { orderLineItemsDtoList: [{ skuCode: 'iphone_13', price: 200, quantity: 1 }] }
    : [{ sku: 'ENG-V8-500', price: 8500, quantity: 1 }];
  const response = http.post(base + (implementation === 'java' ? '/api/order' : '/orders'),
    JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } });
  const ok = response.status === 201 && (implementation !== 'java' || response.body.includes('Placed Successfully'));
  accepted.add(ok ? 1 : 0);
  failed.add(ok ? 0 : 1);
  check(response, { 'order accepted': () => ok });
}
