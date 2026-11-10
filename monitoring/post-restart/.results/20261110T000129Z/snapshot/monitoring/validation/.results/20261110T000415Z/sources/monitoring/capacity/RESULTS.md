# Sustainable-throughput results, November 7, 2026

Later allocation controls, three paired repeats and matched 30-minute trials are
reported in [VALIDATION_RESULTS.md](VALIDATION_RESULTS.md). Those results show the
importance of Java CPU allocation and qualify the single-series bounds below.

Completed: the continuously protected comparison and its upper-rate follow-up are finished. Both applications have their original resources, images, templates and replica counts restored, and real-order checks pass. Grafana and all three node metrics targets are healthy.

Open the local visual report: `.results/series-20261107T210148Z/comparison.html`. It links every current measured step, failed probe, raw report and resource sample. The report's Grafana link selects the full comparison time range.

## Confirmed five-minute rates

Each implementation had a **2-core / 4-GiB application budget**, 250 order workers, a ten-second request timeout and no retries. A pass requires **p95 below 100 ms, zero failed requests, zero dropped arrivals and unchanged ready pods**.

| Implementation | Confirmed orders/s | Accepted orders | p95 at that rate | Errors / dropped |
| --- | ---: | ---: | ---: | --- |
| Java | 200 | 60,001 | 25.36 ms | 0 / 0 |
| Wasm | 400 | 120,001 | 77.34 ms | 0 / 0 |

These are the highest **verified tested rates**, not exact maxima or repeated-run confidence estimates. Latencies in this table correspond to different offered rates. Java's next tested step was 300/s; Wasm's was 450/s. Untested rates within those gaps remain unresolved.

## Where the gates failed

| Implementation | Offered orders/s | Accepted issued requests | p95 | Dropped arrivals | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| Java | 300 | 36,000 / 36,000 | 204.15 ms | 0 | Latency target missed |
| Wasm | 450 | 53,593 / 53,593 | 423.15 ms | 408 | Latency and arrival targets missed |

Every issued request in these probes succeeded. The dropped arrivals at 450/s make that point inconclusive about an intrinsic application ceiling: generator limits, shared dependencies and the VM can contribute. It failed the defined usability test. The accepted-count/nominal-load-duration rate there was 446.61/s, below the offered 450/s.

Java passed the two-minute steps at 25, 50, 75, 100, 150 and 200/s before missing at 300/s. Wasm passed 200, 250, 300, 350 and 400/s, then missed at 450/s in the matched follow-up. Each child began with a three-minute warmup at 25/s. Both passing candidate rates were checked separately for five minutes. No failed trial was replaced with a passing retry at the same rate.

## Interpretation and scope

Wasm verified twice Java's offered rate under this particular allocation. **Java had four independent 500-millicore / 1-GiB service limits; Wasm pooled its 2 cores / 4 GiB inside one runtime.** A busy Java service cannot borrow unused quota from an idle service. An optimized Java allocation was not evaluated, so the budget split is a material part of this result.

This measures accepted-order throughput of the complete local request paths with application-container limits. Shared databases, messaging, Wasm's NATS sidecar, control plane and monitoring are outside the application budget. It does not establish whole-machine efficiency or a general language ranking. The local Docker VM has ten shared CPU cores; the three k3d nodes do not provide thirty independent cores.

Actual running image IDs match the frozen validated setup. The host is `platform-poc/wasmcloud:1.9.2-regack.1`, with HTTP provider `http-server:0.25.0-regack.1`, the subscriber-registration acknowledgement transport patches, a five-second execution limit and four-second RPC limit. The benchmark workload and budgets were unchanged.

## Sleep interruption and retained earlier attempts

macOS logged **Idle Sleep between runs**, after individual run guards exited, at 06:48:13 and 20:36:10 despite mains power and an open lid. Subsequent commands ran during DarkWake while Docker/Kubernetes were unavailable. OrbStack was restarted with explicit user approval, saved resource settings were restored, and functional checks passed before fresh measurements.

A continuous session guard now covers preflight, rollouts, loads, restoration, inter-run gaps and final verification. `run_series.py` retains a guard across both child runs; custom multi-command orchestration must protect the whole sequence. No global macOS power setting was changed. A transient node kubelet certificate/address mismatch after restart recovered automatically; no certificate-validation bypass or extra node restart was used.

Earlier evidence remains intact and is kept separate from the latest comparison:

- `.results/20261107T062044Z`: initial coarse screen. Java's 25/s latency failure and Wasm's 400/s failure did not reproduce in the protected comparison. Its earlier Wasm 200/s confirmation remains a historical observation.
- `.results/20261107T070407Z`: interrupted Java resource rollout, before warmup or measurement. `recovery.json` records the later verified restoration; no throughput is inferred from this attempt.
- `.results/20261107T201446Z`: post-restart lower Java ladder, including a successful 20/s five-minute check. Its final restoration passed.
- The subsequent higher-rate preflight failed before creating a checkpoint or applying resources when idle sleep recurred. `inter-run-sleep-diagnosis.json` in the lower-run directory records the sleep events and mitigation.

Sleep explains the inter-run API outages. Those sleep events followed the completed load phases; they do not isolate the cause of every earlier latency difference. Earlier poor latency is retained rather than attributed conclusively to sleep or pooled into the protected series.

## Evidence and reproduction

- Protected series: `.results/series-20261107T210148Z/series.json`.
- Java source: `.results/20261107T210149Z/report.html`.
- Wasm source: `.results/20261107T212739Z/report.html`.
- Wasm upper probe: `.results/20261107T214847Z/report.html`.
- `comparison.json` hashes all three source reports and retains all their measured steps. Its bars only use passing confirmations lasting at least five minutes; a failed higher-rate follow-up cannot supersede an established passing bound.
- `integrity.json` verifies all 1,095 retained historical files, all seven frozen Wasm registry artifacts and the executable benchmark source hashes remained unchanged.
- `final-restoration.json` verifies deployment templates/images/replicas, HostConfig, component counts, providers, configuration/routing/secret hashes and monitoring.
- Capacity/reporting and budget unit suites pass: 24 tests, including partial-application recovery, continuous guard ordering, prevention of peer load after failed restoration, protocol mismatch checks and exclusion of failed/short confirmations.

Follow [README.md](README.md) to repeat the series, extend a passing top step and regenerate the offline report with `--extra-report`. Local result directories are ignored by Git; source code and protocol instructions remain reviewable. The capacity dashboard's legacy aggregate memory panels can temporarily include terminated-pod samples after rollouts; use the archived current per-container samples for this experiment's memory context.
