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
const orderVUs = Number(__ENV.ORDER_VUS || 250);
for (const value of [idle, load, recovery, rate, orderVUs]) {
  if (!Number.isInteger(value) || value < 1) throw new Error('Durations and ORDER_RATE must be positive integers');
}
const base = (__ENV.BASE_URL || (implementation === 'java' ? 'http://localhost:8081' : 'http://localhost:8080')).replace(/\/$/, '');
const accepted = new Counter('demo_orders_accepted');
const failed = new Counter('demo_order_failures');
const phase = new Gauge('demo_phase');
const target = new Gauge('demo_target_rps');
let failureSampleLogged = false;
let startAnnounced = false;
export const options = {
  tags: { implementation, testid: __ENV.RUN_ID || 'manual' },
  thresholds: { demo_order_failures: ['count==0'], dropped_iterations: ['count==0'] },
  scenarios: {
    orders: { executor: 'constant-arrival-rate', exec: 'order', rate, timeUnit: '1s',
      startTime: `${idle}s`, duration: `${load}s`, preAllocatedVUs: orderVUs, maxVUs: orderVUs, gracefulStop: '30s' },
    // One additional worker emits phase markers; it sends no HTTP requests.
    timeline: { executor: 'constant-vus', exec: 'timeline', vus: 1,
      duration: `${idle + load + recovery}s`, gracefulStop: '2s' },
  },
};
if (__ENV.P95_MS) {
  const limit = Number(__ENV.P95_MS);
  if (!Number.isFinite(limit) || limit <= 0) throw new Error('P95_MS must be positive');
  options.thresholds['http_req_duration{scenario:orders}'] = [`p(95)<${limit}`];
}
export function timeline() {
  if (!startAnnounced) {
    console.log(JSON.stringify({ event: 'demo_started', start_ms: execution.scenario.startTime }));
    startAnnounced = true;
  }
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
    JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' }, timeout: __ENV.REQUEST_TIMEOUT || '60s' });
  const ok = response.status === 201 && (implementation === 'java'
    ? response.body.includes('Placed Successfully') : response.body.trim() === '201 Created');
  accepted.add(ok ? 1 : 0);
  failed.add(ok ? 0 : 1);
  if (!ok && !failureSampleLogged) {
    failureSampleLogged = true;
    console.warn(JSON.stringify({event: 'demo_order_failure_sample', implementation,
      status: response.status, error: response.error, duration_ms: response.timings.duration,
      body: String(response.body || '').slice(0, 500)}));
  }
  check(response, { 'order accepted': () => ok });
}
