# Frozen validation, November 6–7, 2026

Campaign: `.results/20261106T204859Z`. Open `analysis.html` for the visual comparison and every attempt, `report.html` for the execution journal, and `history.html` / `history-review.json` for historical classifications. All 65 historical runs (1,095 files) were retained unchanged. No historical trials were pooled into the fresh validation.

The completed campaign contains five alternating footprint pairs, three memory-density sweeps per implementation, and five cold/cached startup pairs per implementation. No startup pairs were excluded. Medians below summarize separate runs, not independent fresh process instances for the footprint suite.

| Comparison | Java | Wasm |
| --- | --- | --- |
| Idle application memory, median | 2,732 MiB | 293 MiB |
| Loaded application memory, median | 2,739 MiB | 323 MiB |
| Loaded CPU cores, median | 0.275 | 0.511 |
| Order p95 at 100/s, median [range] | 21.58 [20.94–22.92] ms | 35.62 [30.03–50.94] ms |
| Smallest allocation passing all three density sweeps at 25/s | 2,048 MiB | 512 MiB |
| Image-cold first accepted order, median [range] | 24.32 [22.65–25.28] s | 16.29 [13.34–17.36] s |
| Image-cached first accepted order, median [range] | 15.11 [14.80–25.66] s | 8.24 [5.35–9.20] s |

Both implementations passed every measured footprint run with no failed or dropped orders. Median paired ratios show Wasm used about 88% less loaded application RAM and 1.86 times the CPU. Java had lower p95 latency in every footprint pair. Wasm's loaded working set rose from 278 to 347 MiB across repetitions; the lower footprint is consistent, but these runs do not establish a flat long-term working set or diagnose a memory leak.

Java passed the 2 GiB total allocation in all three density sweeps; the next tested 1 GiB allocation encountered observed OOMKilled containers in every sweep. Java's budget was divided equally among its four services, so this is a bound for that allocation policy, not an optimized JVM minimum. Wasm passed 384 MiB in two sweeps; the third stopped at 384 MiB because CPU telemetry coverage was insufficient, not because CPU saturation was demonstrated. Wasm passed 512 MiB in all three sweeps. Its two attempts at 256 MiB encountered OOMKilled runtime containers. Original runtime/inconclusive outcomes and the separate OOM evidence are retained.

A hypothetical 8 GiB application-only pool therefore projects four Java allocations versus sixteen Wasm allocations at the consistently passing caps. This is a memory-only projection: sixteen concurrent Wasm instances were not deployed and tested, and infrastructure reservations and CPU requirements reduce usable density. All passing allocations met both the original CPU gate and the conservative combined shared-VM CPU gate.

Startup measures first accepted real order after scaling the complete application from zero onto a fresh owned worker, followed by a restart using that worker's cached container images. Databases and other dependencies remain warm. Wasm component/provider caches are new on both starts. These are container-image cold/cached results, not whole-machine cold boots or automatic-scaling reaction times.

The validated live runtime was `platform-poc/wasmcloud:1.9.2-regack.1`, with HTTP provider `http-server:0.25.0-regack.1`, a five-second execution limit and four-second RPC limit. Both host and provider use the subscriber-registration acknowledgement transport patch. Repository build defaults may name a newer patch tag; `freeze.json`, registry digests and archived source describe the actual tested setup.

The final integrity check confirmed historical files and all seven local registry artifacts were unchanged. A subsequent live verification confirmed original deployment templates, images, replica counts, HostConfig, component counts, provider IDs and configuration hashes were restored. All three original k3d nodes were Ready; temporary startup workers and campaign helpers had exited. Reusable analysis/documentation additions were made only after that source-integrity verification.

Results apply to these application implementations on one 10-core Docker VM on a 16 GiB MacBook Pro. Application totals include the four Java service containers versus the Wasm runtime container and its child providers. Shared databases and control-plane overhead are outside those totals. Density RAM reconstructions exclude stale terminated-pod samples; raw legacy peaks remain archived. These measurements support lower Wasm memory requirements and faster startup here, alongside lower Java loaded CPU and footprint-test latency; they do not establish a universal language/runtime ranking.
