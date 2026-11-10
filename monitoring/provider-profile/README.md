# PostgreSQL provider diagnostics

This separate diagnostic study compares the existing Java and wasmCloud order paths at 100 orders/s. Each receives a 60-second warmup and a 90-second measured run, sequentially, with 250 VUs. It does not rebuild or change application images, schemas, replicas, resource budgets or stored benchmark results.

It captures:

- Executed SQL and PostgreSQL parse/bind/execute duration events, using temporary statement logging with bind-parameter values suppressed.
- PostgreSQL activity and wait-event observations approximately ten times a second (collection pauses briefly while querying Actuator).
- Order-service Hikari pool acquisition timer deltas and sampled active/pending connection gauges. Inventory's corresponding Actuator metrics are not exposed and are recorded as unavailable.
- CPU-counter deltas for the wasmCloud host and each direct native provider, including the pinned PostgreSQL 0.7.2 process. This is process-level attribution, not a function-level flamegraph.

Run while no other benchmarks or database operations are active, with mains power and an open lid. The runner temporarily changes the local PostgreSQL server's `log_min_duration_statement`, `log_line_prefix` and `log_parameter_max_length` through `ALTER SYSTEM` and reload. This affects server-wide logging during the short measurement intervals. The existing overrides and effective settings are saved privately and restored in `finally`. No PostgreSQL restart or statistics reset is needed. Keep the runner alive through cleanup; if killed forcibly, use its `logging-before.json` to restore the prior overrides and reload PostgreSQL before further tests.

```sh
python3 -m venv /tmp/platform-pg-profile-venv
/tmp/platform-pg-profile-venv/bin/pip install 'psycopg[binary]'
/tmp/platform-pg-profile-venv/bin/python monitoring/provider-profile/run.py
python3 monitoring/provider-profile/analyse.py monitoring/provider-profile/.results/TIMESTAMP
```

The current local runner assumes administrator access via `psql`'s normal local connection defaults, the Homebrew PostgreSQL 15 log destination `/opt/homebrew/var/log/postgresql@15.log`, the existing database names, and a free localhost port 18001 for its owned Kubernetes proxy. Outputs are private under `.results/`. A failed initial telemetry preflight is retained separately from a measured study.

Interpretation limits:

- Stock-provider deadpool acquisition duration is **not measured**. PostgreSQL wait events see only connected backends; they cannot see a caller queued in the application pool or wRPC transport.
- Java pool acquisition time includes obtaining/checking out a connection; pending gauges sampled every five seconds can miss brief waits. Timer deltas are means, not p95 values.
- PostgreSQL duration logs give server elapsed time. Explicit transaction commands are separate from business SQL; Wasm's implicit transactions are still real transactions. Do not equate the sum of logged protocol phases with complete database-call latency or CPU time.
- Different volumes of diagnostic logging can introduce unequal overhead. HTTP timings from this study are not substitutes for the uninstrumented benchmark results.
- Sampled idle `ClientRead` means PostgreSQL is waiting for its client and is not evidence of connection-pool saturation.
- Shared NATS processes and PostgreSQL server CPU are outside the host/provider process total.

The profiler restores logging and stops only its own proxy and sleep inhibitor. Its execution journal, raw statements, activity observations, pool snapshots and derived HTML/JSON are retained independently of earlier benchmark campaigns.
