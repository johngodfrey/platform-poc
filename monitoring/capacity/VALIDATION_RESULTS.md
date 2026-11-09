# Allocation controls, repeated boundaries and stability — November 8, 2026

All three requested studies are complete. Both applications have their original
resource settings, templates, images, replica counts and runtime configuration
restored. Functional order checks, Grafana and all three node metrics targets
pass. No system sleep occurred during this task.

The local visual report is
`.results/validation-20261107T233519Z/analysis.html`; its derived JSON hashes and
links the original reports. It includes failed probes, confirmations and the
long runs, plus CPU, memory and cumulative-latency curves.

## 1. Java CPU allocation materially changed the comparison

Both implementations retained a total **2-core / 4-GiB application budget**.
Java kept 1 GiB per service. Only its CPU distribution changed:

| Java service | Original CPU | Alternative CPU |
| --- | ---: | ---: |
| Orders | 500m | 1200m |
| Inventory | 500m | 600m |
| Notifications | 500m | 150m |
| Products | 500m | 50m |

The original split confirmed **200/s for five minutes**, p95 **34.39 ms**, with
60,001 accepted orders and no errors/drops. Its next 250/s probe had p95 233.01 ms
and ten dropped arrivals. Orders was throttled in 17.1% of its CPU scheduling
periods during that probe; inventory and notifications were each below 1%.
Counter intervals include the brief idle/recovery margins, not just load.

The alternative split confirmed **350/s for five minutes**, p95 **26.24 ms**,
with 105,001 accepted orders and no errors/drops. At the matched 200/s short step,
p95 fell from **56.72 to 15.36 ms**. The same total CPU budget therefore produced
substantially different behaviour. This is one orders-only allocation experiment,
not exhaustive Java optimization. Its 50m product limit also made full application
startup take several minutes; it is not a general recommendation for mixed traffic.

The alternative was selected for subsequent trials by the declared rule: use it
only if its confirmed rate was at least the original allocation's rate. Wasm's
allocation, component concurrency and patched transport remained unchanged.

## 2. Three alternating-order pairs found variability near the upper probes

Every child used fresh application pods, three minutes of priming at 25/s, then
three minutes at 100/s, two-minute steps, 250 order workers, a ten-second client
timeout and no retries. A pass required p95 below 100 ms, zero errors/drops,
complete offered work and unchanged ready pods. Highest passing short candidates
received separate five-minute confirmations within the same child.

| Pair/order | Java confirmation | Wasm confirmation |
| --- | --- | --- |
| 1: Java → Wasm | 400/s: pass; p95 22.97 ms | 450/s: fail; p95 281.54 ms; 321 dropped |
| 2: Wasm → Java | 400/s: pass; p95 22.13 ms | 400/s: fail; p95 115.65 ms; zero dropped |
| 3: Java → Wasm | 350/s: pass; p95 21.01 ms | 350/s: pass; p95 60.06 ms |

Every issued request in these confirmations succeeded. Candidate confirmation
rates differ and must not be read as matched-rate latency comparisons.

At the matched **350/s short steps**, all three trials passed for each application:
Java p95 **20.47–25.05 ms**, Wasm **60.31–85.45 ms**. At 400/s, Java's third short
trial dropped 94 arrivals despite p95 38.15 ms; Wasm's third had p95 103.98 ms and
38 drops. The initial alternative-Java screen also dropped 136 at 400/s, although
its p95 was only 43.82 ms. Those failures remain in the report.

This does not establish exact maxima. Dropped work can reflect finite generator
workers or host/application stalls; every dropped arrival was never issued.
Java was not probed above 400/s in the repeat matrix. Wasm's higher 450/s probe
also creates a different history before its lower-rate confirmation, so carryover
may contribute; it was not isolated causally. The failed Wasm 400/s confirmation
averaged about 1.19 runtime CPU cores with only 3/3,020 periods throttled, providing
little evidence that its two-core runtime quota alone caused that miss.

## 3. Matched 30-minute runs at 350/s

350/s was the highest short-step rate passed in all three repeats by both
implementations. Each long trial began with fresh pods and the same priming and
warmup, without first running an upper-rate overload probe.

| Metric | Java | Wasm |
| --- | ---: | ---: |
| Accepted orders | 629,954 | 630,001 |
| HTTP/body failures | 0 | 0 |
| Dropped arrivals | 47 | 0 |
| p95 | 21.77 ms | 51.65 ms |
| Median | 3.99 ms | 11.04 ms |
| Maximum | 1,380.11 ms | 453.59 ms |
| Mean application CPU | 0.662 cores | 1.178 cores |
| Mean application working-set RAM | 1,249 MiB | 323 MiB |
| RAM: first → last five-minute means | 1,215 → 1,288 MiB | 202 → 445 MiB |
| Strict test gate | Failed zero-drop criterion | Passed |

Java's 47 unscheduled arrivals are about **0.0075%** of its planned work. Its
latency target and every issued request passed, but it did not pass the full gate.
Do not describe this as 47 failed HTTP requests or proof of an intrinsic capacity
ceiling. Both applications retained ready, unchanged pods throughout.

Wasm used about **1.78× application CPU** and **74% less average application RAM**
at this matched load. Its memory advantage narrowed during the run: its last
five-minute mean was about 65% below Java's. Wasm RAM grew substantially rather
than reaching a demonstrated plateau. Caches, pools, allocation behaviour or a
leak could contribute; this test does not identify the cause. Do not extrapolate
the earlier short, lower-rate density caps to long-lived 350/s workloads without
explaining this growth. Java's working set also grew, by a smaller amount.

Resource statistics have 100% complete-current-container coverage. CPU uses
supplemental 90-second rates sampled every 15 seconds; original 30-second queries
remain archived. Latency curves are cumulative within each run, not rolling-window
percentiles. A 30-minute run cannot rule out longer-term growth or rare failures.

## Scope, retained adaptations and evidence

This compares the complete local accepted-order request paths under application
container budgets, not languages in isolation or whole-system cost. Wasm native
provider child processes are included in its host container. Its NATS sidecar,
shared databases/messaging, operator, ingress, monitoring and generator are outside
the application budget. All three k3d nodes share one ten-core Docker VM.

The frozen host remains `platform-poc/wasmcloud:1.9.2-regack.1`, HTTP provider
`0.25.0-regack.1`, subscriber-registration acknowledgement transport patches,
five-second execution limit and four-second RPC limit. Images, artifacts,
component counts and provider identities match the prior frozen setup.

Adaptations and failed attempts are explicit:

- `.results/validation-20261107T230354Z`: direct-to-100/s startup warmup failed;
  resources restored. The two-stage warmup was then declared before the new series.
- `.results/validation-20261107T230851Z`: original-allocation screen completed;
  alternative setup exceeded the four-minute product rollout wait before load.
  Resources restored; setup failure retained.
- `.results/validation-20261107T233519Z`: continued with the verified original
  screen and a ten-minute bounded rollout wait. Measurement settings/core sources
  matched; the setup-only revision and inherited source are journalled.
- `stability-addendum.json` was declared after the first failed highest-candidate
  confirmation, before the remaining pairs. For automatically skipped long trials,
  it selects the highest short step passed in all three repeats, capped at 400/s,
  and runs it once without lowering the rate and retrying. This selected Wasm 350/s.
  The original matrix journal and failed confirmations were not rewritten.

`final-restoration.json` verifies all restoration/configuration checks, seven
frozen registry artifacts, all 1,095 retained historical files and absence of
sleep. `source-integrity.json` and the follow-up source snapshot verify executable
provenance. The capacity/reporting and budget suites pass **34 unit tests**;
`git diff --check` passes. The running setup was restored; the alternative resource
weights were not left deployed.

Follow [README.md](README.md) for commands, protocol details and report generation.
