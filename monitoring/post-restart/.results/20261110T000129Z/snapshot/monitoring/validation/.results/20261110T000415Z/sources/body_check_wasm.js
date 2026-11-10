import { check } from "k6";
import crypto from "k6/crypto";
import http from "k6/http";

const baseUrl = (__ENV.BASE_URL || "http://localhost:8080").replace(/\/$/, "");
// ASCII payload, identical to the order test: string length equals UTF-8 bytes.
const payload = JSON.stringify([{ sku: "ENG-V8-500", price: 8500, quantity: 1 }]);
const expectedSha256 = crypto.sha256(payload, "hex");
let failureSampleLogged = false;

export const options = {
  thresholds: {
    checks: ["rate==1"],
    http_req_failed: ["rate==0"],
    dropped_iterations: ["count==0"],
  },
  scenarios: {
    body_check: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 100),
      timeUnit: "1s",
      duration: __ENV.TEST_DURATION || "2m",
      preAllocatedVUs: 100,
    },
  },
};

export default function () {
  const response = http.post(`${baseUrl}/body-check`, payload, {
    headers: { "Content-Type": "application/json" },
  });
  let result;
  try {
    result = response.json();
  } catch (_) {
    // A transport error may return plain text or no response at all.
  }

  const passed = check(response, {
    "body-check returned 200": (r) => r.status === 200,
    "body byte count matches": () => result?.bytes === payload.length,
    "body SHA-256 matches": () => result?.sha256 === expectedSha256,
  });
  if (!passed && !failureSampleLogged) {
    failureSampleLogged = true;
    console.warn(JSON.stringify({
      event: "body_check_failure_sample",
      timestamp: new Date().toISOString(),
      status: response.status,
      error: response.error,
      duration_ms: response.timings.duration,
      expected_bytes: payload.length,
      expected_sha256: expectedSha256,
      body: String(response.body || "").slice(0, 1000),
    }));
  }
}
