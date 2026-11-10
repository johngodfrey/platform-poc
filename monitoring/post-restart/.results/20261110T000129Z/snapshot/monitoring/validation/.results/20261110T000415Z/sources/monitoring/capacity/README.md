# Maximum sustainable order throughput

See [VALIDATION_RESULTS.md](VALIDATION_RESULTS.md) for the completed CPU-allocation
control, three alternating pairs and matched 30-minute results, including failed
gates and observed Wasm memory growth.

For the boundary-refinement series used after the initial coarse screen:

```sh
python3 monitoring/capacity/run_series.py
```

This searches Java at 25, 50, 75, 100, 150, 200, 300 and 400/s, then Wasm at
200, 250, 300, 350 and 400/s, stopping each ladder at its first failed step.
The finer grids follow the independently observed bounds; resource allocations,
durations, workers and pass criteria below remain identical. Each implementation
starts with its own warmup and confirms its highest passing step for five minutes.
The series retains a single wake guard across preflight, rollouts, loads,
restoration and gaps between child runs. Its `series-<timestamp>/series.json`
links the raw child reports; logs remain alongside it. Each child must verify
restoration and application health before the next starts. This is boundary
refinement, not a substitute for a first coarse screen on a different machine.

After the series completes, generate the combined offline visual report:

```sh
python3 monitoring/capacity/summarize.py monitoring/capacity/.results/series-<timestamp>
open monitoring/capacity/.results/series-<timestamp>/comparison.html
```

The summary checks that budgets and measurement settings match, retains every
measured step and links original reports. Only a passing confirmation of at
least five minutes enters its sustained-throughput bars. Different tested rate
grids can leave overlapping capacity brackets; different confirmed bounds alone
do not establish different true maxima. Each series is one sequential comparison,
not repeated-run confidence evidence.

If Wasm passes the top 400/s step and its confirmation, extend the ladder with
the same profile:

```sh
python3 monitoring/capacity/run.py wasm --budget monitoring/budget/budget.json \
  --rates 450,500,600,800 --seconds 120 --warmup 180 --warmup-rate 25 \
  --p95-ms 100 --vus 250 --confirm-seconds 300
python3 monitoring/capacity/summarize.py monitoring/capacity/.results/series-<timestamp> \
  --extra-report monitoring/capacity/.results/<follow-up-timestamp>
```

`--extra-report` retains all matched follow-up stages, including failed
confirmations. Its sustained bars use the highest observed passing five-minute
confirmation, so a failed higher-rate follow-up cannot replace an established
passing bound. Source reports are hashed and duplicate inputs rejected.
The same extension procedure applies to Java when its top tested rate passes.

Sleep protection matters **between runs too**: macOS may enter idle sleep on
mains with an open lid as soon as a per-run guard exits. This occurred between
the November 7 attempts, leaving Docker/Kubernetes unavailable during DarkWake.
The completed load phases preceded those sleep events; interrupted preflights
are retained separately. For custom multi-command series, wrap the entire
orchestrator in `caffeinate -i -s`, rather than protecting only individual loads.
No global macOS power settings are changed.

From the repository root, with the existing Java/Wasm deployment and monitoring stack healthy, mains connected and the laptop lid open:

```sh
python3 monitoring/capacity/run.py both \
  --budget monitoring/budget/budget.json \
  --rates 25,50,100,200,400,800,1600 \
  --seconds 120 --warmup 180 --warmup-rate 25 \
  --p95-ms 100 --vus 250 --confirm-seconds 300
```

This applies a total application budget of 2 CPU cores and 4 GiB RAM to each implementation. Java receives 500 millicores and 1 GiB per service; Wasm receives the full budget in its runtime container. Providers running as runtime child processes are included. Shared databases, messaging, Wasm sidecar and monitoring are outside the application budget. The budget split can constrain a busy Java service before all its application CPU is used; interpret this as capacity of these allocations, not a universal runtime limit.

Both applications use the same rate ladder, 250 order workers, ten-second request timeout and no retries. Each application warms at 25/s for three minutes, then runs two-minute rate steps until the first failure. Each step requires p95 below 100 ms, zero failed requests, zero dropped arrivals, the intended request count and unchanged ready pods. A separate five-minute run checks the highest passing tested step. A failed confirmation means that rate has not demonstrated five-minute sustainability, even if its shorter step passed. If all ladder steps pass, extend the ladder: the top rate is only a lower bound.

Open the [live capacity dashboard](http://localhost:8080/grafana/d/java-wasm-capacity). Select this run's `capacity-...` IDs; warmup and confirmation have separate IDs. Latency charts show cumulative statistics within each stage. The saved auto-refreshing `monitoring/capacity/.results/<UTC timestamp>/report.html` shows each stage and its confirmation, with raw k6 summaries/logs and sampled current per-container CPU/RAM alongside it. Legacy aggregate memory dashboard panels may temporarily include terminated-pod samples after resource rollouts; use the saved per-container samples to assess memory.

Original resource settings are backed up before changes and restored in `finally`, followed by functional checks of both applications. Idle sleep is inhibited while running. A closed lid or loss of mains interrupts load sampling, retains partial evidence and enters restoration. If the process is killed and cleanup cannot complete:

```sh
python3 monitoring/budget/run.py restore monitoring/capacity/.results/<timestamp>/state.json
```

Dropped work is classified as inconclusive about application capacity because generator limits or host pressure can cause it. Shared dependencies, ingress and the local Docker VM may constrain either implementation. First failing steps are bounded overload probes; this does not deliberately keep a failed deployment overloaded. Refine between a confirmed passing rate and the next failing rate using identical settings for both implementations, and repeat in reverse implementation order before making a precise capacity claim.

Without `--budget`, the runner preserves current resources. Without `--confirm-seconds`, it performs only the short ladder screen. Older reports therefore describe different protocols and should not be pooled with this run.

## Allocation, repeated boundaries and sustained stability

To run all three follow-up controls in one continuously protected sequence:

```sh
python3 monitoring/capacity/validate.py
```

Allow several hours on mains with the lid open. The continuously refreshing
`.results/validation-<timestamp>/report.html` links the execution journal;
`validation.json` retains commands and raw report paths. Use the existing live
capacity dashboard and select the new `capacity-...` run IDs.

The sequence records its protocol and source snapshots before measurements:

1. Screen Java with the original 500m CPU per service, then a declared alternative:
   orders 1200m, inventory 600m, notifications 150m, products 50m. Both retain
   2 cores total and 1024 MiB per service. Wasm remains unchanged. Test
   100, 200, 250, 300, 350, 400, 450, 500 and 600/s until the first failed step,
   and confirm the highest passing step for five minutes. Prefer the alternative
   for subsequent comparisons only if its confirmed rate is at least as high.
   This checks sensitivity to one allocation; it is not exhaustive optimization.
2. Repeat three Java/Wasm pairs in Java–Wasm, Wasm–Java, Java–Wasm order.
   Java retests the selected confirmed rate and next observed higher probe;
   Wasm tests 350, 400 and 450/s. Short steps last two minutes and the best
   passing step receives its own five-minute confirmation. Keep every failed
   step and confirmation. A child infrastructure/restoration failure stops the
   sequence rather than silently retrying it.
3. Run each application for 30 minutes at the lowest successfully confirmed
   rate across its three repeats. If any repeat has no passing confirmation,
   skip that implementation's stability test and retain the reason.

Every child starts from freshly rolled application pods, primes for three minutes
at 25/s and then warms for three minutes at **100/s**, uses 250 workers, a ten-second request timeout, no retries, and the
same p95 <100 ms / zero errors / zero drops gates. The higher warmup rate is
intentional; keep this series separate from earlier 25/s-warmup observations.
Restoration and real-order checks must pass after each child before the next.

Each stage archives `telemetry.json`: raw kubelet CFS counters before/after,
plus 15-second CPU and working-set-memory queries restricted to its current
pods. Counter intervals include the idle and recovery margins; do not call them
load-only throttling. Telemetry errors remain explicit. The source benchmark and
capacity/budget drivers are snapshotted and hashed; this wrapper adds observation
without changing HTTP traffic. Grafana's k6 p95 trends are cumulative within a
stage, not rolling-window percentiles. A 30-minute aggregate pass does not prove
every short interval met the latency target or exclude slower long-term memory
growth.

The first direct-to-100/s warmup attempt is retained separately at
`.results/validation-20261107T230354Z`: it failed during startup and restored its
resources. The two-stage warmup protocol was declared after that failure, before
the fresh comparison. Do not treat that initial warmup as measured capacity.

After the validation sequence, build its offline resource-trend report:

```sh
python3 monitoring/capacity/analyse_validation.py monitoring/capacity/.results/validation-<timestamp>
open monitoring/capacity/.results/validation-<timestamp>/analysis.html
```

The derived JSON hashes its source reports, retains failed gates, and records
CPU/memory sample coverage. Memory trends require all current application
containers at each timestamp; missing containers are never counted as zero.
CFS parsing accepts kubelet sample timestamps and computes counter deltas rather
than interpreting those timestamps as values.

A 50m product-service limit can make its Spring startup exceed the original
four-minute rollout wait. The validation wrapper allows a bounded ten-minute
rollout wait before measuring; component execution and HTTP timeouts are
unchanged. The first such setup failure is retained under
`.results/validation-20261107T230851Z`, with restoration verified.
To continue from that completed original-allocation baseline without repeating it:

```sh
python3 monitoring/capacity/validate.py --resume monitoring/capacity/.results/validation-20261107T230851Z
```

This creates a fresh journal, references the original report, checks that all
prior children restored successfully and that the measurement protocol and core
benchmark sources match. It reruns the alternative allocation and all later
studies. The only permitted protocol difference for this continuation is the
setup rollout wait; previous journals and failed attempts are preserved.

The current series also retains `stability-addendum.json`, declared after the
first Wasm 450/s five-minute confirmation failed despite a passing short step.
The base wrapper would then skip its long run. To complete the requested
stability study, any automatically skipped implementation receives one separate
30-minute follow-up at the highest short-step rate that passed all three current
repeats, capped at 400/s. Both implementations have supporting independent
five-minute observations at that cap; the older Wasm observation used a different
warmup and is not pooled into current-repeat statistics. The long follow-up
itself verifies or rejects stability. Retain failure without lowering the rate
and retrying, and do not run if there is no common passing short step. This
selection adaptation is recorded separately; original journals and failed
higher-rate confirmations remain unchanged.

Execute the retained addendum with:

```sh
python3 monitoring/capacity/finish_stability.py monitoring/capacity/.results/validation-20261107T233519Z
```

It can be started while the matrix runs: it holds a wake guard, waits for safe
matrix completion, then runs skipped long studies sequentially. It refuses
already-started addenda, unrestored/missing repeats, changed frozen sources or
absence of a common passing step. It never replaces an existing failed long
trial. Its source snapshot, exact commands, raw reports and outcomes are recorded
in the addendum, which the combined analysis includes separately from the three
highest-candidate confirmations.

Once both the matrix and addendum finish, optionally archive the cumulative
latency curves and smoother CPU observations before Prometheus retention expires:

```sh
python3 monitoring/capacity/collect_stability.py monitoring/capacity/.results/validation-<timestamp>
python3 monitoring/capacity/analyse_validation.py monitoring/capacity/.results/validation-<timestamp>
```

Original loads, reports and 30-second CPU queries remain intact. The supplement
uses 90-second CPU rates so idle containers with sparse cAdvisor updates can
contribute complete application totals; coverage is still reported. Source
telemetry/report hashes bind the supplement to its original trial. Latency curves
are labelled as cumulative p95, and only a single matching series is plotted;
percentiles from multiple status-labelled series are never averaged or summed.
