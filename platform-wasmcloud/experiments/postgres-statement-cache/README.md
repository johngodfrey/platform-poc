# PostgreSQL connection-aware statement cache

An isolated patch to wasmCloud PostgreSQL provider **0.7.2**, from checksum-pinned wasmCloud **v1.5.1** sources. It changes the `wasmcloud:postgres/query` path to prepare through the checked-out connection's `deadpool-postgres 0.13.2` cache, then execute the resulting statement on that same connection. The component interfaces, SQL parameter conversion and row decoding remain unchanged.

The patch also explicitly enables ULID's `std` feature in the provider manifest. Its existing `Ulid::new()` call does not compile in an isolated feature-selected build without that feature; the full upstream workspace normally obtains it through dependency feature unification. No dependency version or lockfile is changed.

The existing host, HTTP registration-acknowledgement patches, running PostgreSQL provider, component binaries and historical benchmark artifacts are not replaced by the build. The PostgreSQL transport dependency stays at its unmodified upstream version, so caching is the provider code change. The application requires no recompilation to use this provider.

Behavior:

- Statements belong to the physical connection, so returning a connection to the pool preserves reuse and a replacement connection starts with an empty cache.
- Each physical connection caches at most **128 statement shapes** through this query path. At capacity, its cache is cleared before preparing the next statement. This is coarse eviction, not LRU. It limits entry count, not total SQL-text bytes.
- Invalid statement-name and feature-not-supported errors clear that connection's cache. This covers `DEALLOCATE ALL` and cached-plan result-type changes. The current call still returns its error; a later call can prepare afresh.
- The provider never retries a failed query automatically, including writes.
- Invalid SQL is not cached. Constraint errors are returned without clearing unrelated valid statements.
- Raw batch execution and the explicit prepared-statement token API are unchanged.

The application's order insert currently interpolates line-item contents into its SQL text. The fixed benchmark request can reuse its statement, but varied orders can create different cache keys. Fully binding those values would improve reuse for mixed workloads; that is a separate application change.

## Build

From this directory, with Docker and `wash` installed:

```sh
caffeinate -i -s sh build.sh
```

On other systems, omit `caffeinate`. The build reuses an existing verified source archive if available or downloads it. It extracts into its own ignored `.build/`, applies `patches/provider.patch` with zero fuzz, and records input checksums in `.build/provenance.json`. Source hashes are checked on subsequent builds. Dependency resolution uses upstream `Cargo.lock` with `--locked`.

Outputs:

- `.build/artifacts/sqldb-postgres-provider`: native Linux provider for the Docker engine's architecture.
- `.build/artifacts/sqldb-postgres-0.7.2-stmtcache.1.par.gz`: signed experimental provider archive with separate keys under `.build/keys/`.
- `.build/artifacts/checksums.json`: output hashes.

Building does not publish the archive or edit the live `Application`. A deployment trial should publish under a new OCI reference, retain the original `ghcr.io/wasmcloud/sqldb-postgres:0.7.2` reference for rollback, change only the database provider, and record both artifact digests. Do not overwrite the original tag or rebuild the host as part of that comparison.

## Integration tests

Tests use the exact `cached_statement.rs` helper copied into the provider. They create only owned database connections and temporary tables, and terminate only their own connection for the reconnect test. Set a local PostgreSQL connection explicitly:

```sh
CACHE_TEST_PG_CONFIG='host=/tmp dbname=postgres application_name=statement-cache-integration' \
  cargo test --locked --manifest-path tests/Cargo.toml -- --test-threads=1
```

Coverage includes same-connection reuse with different bind values, separate physical connections, pool checkout, bounded dynamic SQL, invalid SQL, invalid statement names, result-schema changes, reconnects and no replay of failed inserts.

The source patch alone does not establish a performance improvement. After deployment, repeat the provider profiling capture: warmed repeated SQL should stop generating one Parse per execution, while result correctness and order acceptance stay unchanged. Then compare CPU and latency without diagnostic statement logging. Do not combine those new measurements with the existing post-restart baseline.

The [initial completed comparison](RESULTS.md) found approximately 4.5% less
application CPU and 20% less PostgreSQL-provider CPU in two matched local pairs.
The original provider was restored afterward.

## Controlled performance comparison

`build-control.sh` builds an uncached provider from the same upstream source, lockfile, compiler, ULID feature and optimisation flags. Only the statement-cache changes are absent. Build both archives, then publish them under separate local tags:

```sh
caffeinate -i -s sh build-control.sh
wash push --insecure localhost:5001/v2/platform-poc/sqldb-postgres:0.7.2-stmtcache.1 .build/artifacts/sqldb-postgres-0.7.2-stmtcache.1.par.gz
wash push --insecure localhost:5001/v2/platform-poc/sqldb-postgres:0.7.2-stmtcache-control.1 .build/artifacts/control/sqldb-postgres-0.7.2-stmtcache-control.1.par.gz
```

Run `compare.py` with a Python environment containing `psycopg`. It takes a stock baseline and two uncached/cached pairs, each with a fresh Wasm host, 60-second warmup and 120-second measurement at100/s. CPU is measured directly from the host/provider processes. Small SQL probes run separately, with temporary PostgreSQL logging, to verify Parse/Execute behavior. Results are retained beneath `.build/performance/`. The test needs mains power and an open lid; no other load test should run concurrently.

```sh
python3 compare.py
python3 analyse_compare.py .build/performance/TIMESTAMP
```

This temporarily deploys new wadm application versions and restarts only the Wasm host. It keeps component counts, host settings, resources and Java unchanged. It restores the original saved application version/provider and verifies the original provider's executable hash, Kubernetes specs and database logging afterward. Private manifests and a restoration snapshot are written with restrictive permissions; do not publish those files. If interrupted forcibly, restore the saved original wadm application version before restarting the host. The original Kubernetes Application resource is not edited.
