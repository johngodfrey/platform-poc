# Initial performance comparison — November 10, 2026

The prepared-statement cache worked in the real application and improved both
matched pairs at 100 orders/s. These are short local trials, not a capacity or
long-term stability claim.

Each phase used a fresh Wasm host, 60 seconds of warmup and 120 seconds measured,
with 250 order workers and unchanged component counts/resources/execution limits.
PostgreSQL statement logging was disabled during the performance intervals.

Medians across two fresh-host trials per same-build variant:

| Metric | Uncached control | Cached | Reduction |
| --- | ---: | ---: | ---: |
| Host plus native-provider CPU | 0.399 cores | 0.381 cores | 4.5% |
| PostgreSQL-provider CPU | 0.0553 cores | 0.0442 cores | 20.0% |
| Application CPU-seconds / 1,000 accepted orders | 4.089 | 3.903 | 4.5% |
| Median latency | 7.85 ms | 6.50 ms | 17.2% |
| Per-run p95 latency | 17.34 ms | 14.47 ms | 16.5% |

All four matched phases completed about 12,000 accepted orders each, with zero
failed checks and zero dropped arrivals. The ratios compare medians of run
statistics; percentiles were not pooled or averaged across individual requests.

A separate 10-second SQL probe, after the timed intervals, found:

| Variant | Parse events/order | Execute events/order |
| --- | ---: | ---: |
| Stock provider | 2 | 2 |
| Same-build uncached control | 2 | 2 |
| Cached provider | 0 | 2 |

This confirms warmed statement reuse without removing the underlying database
operations. The uncached control and cached variant use the same upstream source,
compiler, dependency lock, optimisation flags and ULID build feature. A separate
stock observation used 0.423 application cores and had 23.57 ms p95; differences
against stock cannot be attributed solely to caching because build differences
and run variation also apply.

The cached runs' mean application working sets were 271 and 277 MiB, versus 281
and 274 MiB for the controls. This does not establish a memory improvement or a
long-term plateau. Shared PostgreSQL/NATS CPU and whole-machine energy are outside
the application CPU total.

The first attempt retained a deployment-only error when re-uploading an existing
wadm version. Its completed stock/control/cached measurements were preserved.
The continuation changed lifecycle handling only, verified unchanged benchmark
code and binaries, and ran only the second pair. It also removes its own
undeployed model versions during cleanup: the Application API exposes the latest
stored version, which need not be the actively deployed one.

Both the runtime and Application API were restored to the original
`ghcr.io/wasmcloud/sqldb-postgres:0.7.2`, application version `v0.0.9-regack.1`.
The original provider executable hash, deployment/HostConfig specs and PostgreSQL
logging settings were verified restored.

Evidence, retained under ignored `.build/performance/`:

- First attempt: `20261110T142553Z/report.json`, with retained setup failure and
  separate verified `recovery.json`.
- Completed continuation: `20261110T144343Z/analysis.html`, `analysis.json`,
  `report.json`, source hashes and raw per-phase k6 files.

The result supports a modest application-level CPU improvement and a larger
provider-specific saving. It does not establish Java CPU parity, maximum
throughput, or a whole-server energy saving.
