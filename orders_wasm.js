import { check } from "k6";
import http from "k6/http";
import { Counter } from "k6/metrics";

const orderResponses = new Counter("order_responses");
const orderFailures = new Counter("order_failures");
const orderTimeouts = new Counter("order_client_timeouts");
const transportFailures = new Counter("order_transport_failures");
let failureSampleLogged = false;
const baseUrl = (__ENV.BASE_URL || "http://localhost:8080").replace(/\/$/, "");
const orderVUs = Number(__ENV.ORDER_VUS || 250);
if (!Number.isInteger(orderVUs) || orderVUs < 1) {
  throw new Error("ORDER_VUS must be a positive integer");
}

// Materialize status submetrics in the summary without imposing a status quota.
const thresholds = {
  order_failures: ["count==0"],
  dropped_iterations: ["count==0"],
};
for (const status of [0, 201, 400, 404, 500, 502, 503, 504]) {
  thresholds[`order_responses{status:${status}}`] = ["count>=0"];
}

export const options = {
  thresholds,
  scenarios: {
    orders: {
      executor: "constant-arrival-rate",
      rate: 100,
      timeUnit: "1s",
      duration: __ENV.TEST_DURATION || "5m",
      preAllocatedVUs: orderVUs,
      maxVUs: orderVUs,
    },
  },
};

export default function () {
  const response = http.post(
    `${baseUrl}/orders`,
    JSON.stringify([
      {
        sku: "ENG-V8-500",
        price: 8500,
        quantity: 1,
      },
    ]),
    {
      headers: {
        "Content-Type": "application/json",
      },
    },
  );

  const failed = response.status !== 201;
  orderResponses.add(1, { status: String(response.status) });
  orderFailures.add(failed ? 1 : 0);
  orderTimeouts.add(response.error_code === 1050 ? 1 : 0);
  transportFailures.add(response.status === 0 ? 1 : 0);

  // Module state is per VU: log at most one sample per allocated VU.
  // Count every failure, but avoid logging every response under load.
  if (failed && !failureSampleLogged) {
    failureSampleLogged = true;
    console.warn(JSON.stringify({
      event: "order_failure_sample",
      timestamp: new Date().toISOString(),
      vu: __VU,
      iteration: __ITER,
      status: response.status,
      error_code: response.error_code,
      error: response.error,
      duration_ms: response.timings.duration,
      body: String(response.body || "").slice(0, 1000),
    }));
  }

  check(response, {
    "order accepted": (r) => r.status === 201,
  });
}
