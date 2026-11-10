import { check } from "k6";
import http from "k6/http";

const baseUrl = (__ENV.BASE_URL || "http://localhost:8080").replace(/\/$/, "");
// Default to the body-check payload; EMPTY_BODY=true isolates empty POSTs.
const payload = __ENV.EMPTY_BODY === "true"
  ? ""
  : JSON.stringify([{ sku: "ENG-V8-500", price: 8500, quantity: 1 }]);
let failureSampleLogged = false;
const traceRequests = __ENV.TRACE_REQUESTS === "true";
const runId = __ENV.RUN_ID || "constant";

export const options = {
  thresholds: {
    checks: ["rate==1"],
    http_req_failed: ["rate==0"],
    dropped_iterations: ["count==0"],
  },
  scenarios: {
    constant_response: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 100),
      timeUnit: "1s",
      duration: __ENV.TEST_DURATION || "2m",
      // Allow the 60-second HTTP timeout to finish for late requests.
      gracefulStop: "65s",
      preAllocatedVUs: 100,
    },
  },
};

export default function () {
  const requestId = `${runId}-${__VU}-${__ITER}`;
  const url = `${baseUrl}/constant-response` +
    (traceRequests ? `?request_id=${encodeURIComponent(requestId)}` : "");
  if (traceRequests) {
    console.log(JSON.stringify({ event: "request_start", request_id: requestId,
      timestamp: new Date().toISOString() }));
  }
  const response = http.post(url, payload, {
    headers: { "Content-Type": "application/json" },
    tags: { name: "POST /constant-response" },
  });
  if (traceRequests) {
    console.log(JSON.stringify({ event: "request_end", request_id: requestId,
      timestamp: new Date().toISOString(), status: response.status,
      error: response.error, timings: response.timings }));
  }
  const passed = check(response, {
    "constant-response returned 200": (r) => r.status === 200,
    "constant response matches": (r) => r.body === "200 OK",
  });
  if (!passed && !failureSampleLogged) {
    failureSampleLogged = true;
    console.warn(JSON.stringify({
      event: "constant_response_failure_sample",
      request_id: requestId,
      empty_body: payload.length === 0,
      timestamp: new Date().toISOString(),
      status: response.status,
      error: response.error,
      duration_ms: response.timings.duration,
      body: String(response.body || "").slice(0, 1000),
    }));
  }
}
