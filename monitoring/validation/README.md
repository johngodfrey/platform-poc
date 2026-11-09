# Frozen validation campaign

From the repository root, on mains power with the laptop lid open:

```sh
python3 -m unittest discover -s monitoring/validation -p 'test_*.py'
python3 monitoring/validation/run.py
```

This retains and hashes historical results, creates a private live-state checkpoint and source archive, then runs the protocol in `protocol.json`: five footprint pairs, three memory-density sweeps per implementation, and five cold/cached startup pairs per implementation. Each suite alternates implementation order. The dashboard remains available during the tests; the campaign report links every attempt and each original report.

Results are under `monitoring/validation/.results/<UTC timestamp>/report.html`. `campaign.json` journals progress; `freeze.json` records actual live image IDs and configuration hashes. `private-baseline.json` and source snapshots are local private artifacts. Do not publish raw state or logs without reviewing for credentials. Historical originals are neither deleted nor edited; `history.json` includes a SHA-256 file inventory and evidence classifications. Derived summaries never count as extra trials. Old footprint HTTP summaries are not CPU/memory measurements.

The completed November 6–7 validation is described in [RESULTS.md](RESULTS.md). Its combined visual report is `.results/20261106T204859Z/analysis.html`; `history.html` and `history-review.json` retain the historical classifications. Local results directories are ignored by Git.

Rebuild the combined report from archived evidence without running benchmarks or contacting the cluster:

```sh
python3 monitoring/validation/analyse.py monitoring/validation/.results/20261106T204859Z
open monitoring/validation/.results/20261106T204859Z/analysis.html
```

`analyse.py` and `reconstruct_memory.py` preserve the supplemental analysis used during this campaign. `--refresh-memory` optionally queries local Prometheus for missing density memory reconstructions, using previously archived pod observations. Without those observations or retained Prometheus history, missing measurements cannot be recovered. Existing reconstructions remain cached; the default report command is offline. These analysis tools were promoted into reusable source after the frozen campaign completed; its original analysis source and benchmark sources remain archived unchanged.

Density's legacy aggregate memory recording rule can include terminated-pod samples for several minutes after a rollout. The supplemental reconstruction requires fresh samples (at most 35 seconds old) pinned to observed live application pods, with complete container coverage. Use those reconstructed measurements rather than legacy memory peaks. This issue does not affect allocation, HTTP, restart or CPU gates. The running dashboard was not changed during validation. Shared-VM CPU adjudication additionally sums simultaneous k3d node CPU fractions: the three nodes share one Docker VM, rather than three independent CPU budgets. Missing coverage is inconclusive, not proof of CPU saturation.

The campaign refuses source/configuration drift. Resource changes and temporary fresh workers are allowed only within the existing guarded density/startup protocols, which restore their checkpoints. It does not upgrade the live runtime to repository defaults. The host/provider transport fixes must already be deployed, with 5-second execution and 4-second RPC limits. Application image IDs and live provider/component inventory are frozen, including differences from repository defaults.

Footprint uses the application containers (all four Java services versus the Wasm runtime and its child providers). Separate namespace telemetry supplies context. Shared databases, host services and control-plane overhead are outside application totals. CPU uses complete 60-second windows inside each phase; missing/stale per-container samples invalidate a resource comparison rather than becoming zero. All failed requests and dropped arrivals remain reported.

Density reports the smallest **passing tested allocation**, its next tested lower outcome, and a memory-only slot projection. Readiness failure or pod replacement alone does not prove memory exhaustion. Cold/cache startup means container-image cache state on isolated new workers, with databases and other dependencies already running; it is not a whole-machine cold boot.

The macOS wake guard prevents idle sleep while this command runs. A closed lid or loss of mains interrupts the campaign. If interrupted, use the recovery command printed in the child suite's `driver.log`; density uses `python3 monitoring/density/run.py restore <state.json>`, startup uses `python3 monitoring/startup_cache/run.py restore --state <state.json>`. Do not resume measurements across a source/configuration change: start a new campaign and retain the old one.
