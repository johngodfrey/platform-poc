# Wasm alongside Java in the existing k3d cluster

This setup reuses `k3d-platform-poc` and its registry on port 5001. Java runs in
`java`; wasmCloud's operator, NATS, wadm, runtime and application run in `wasm`.
The operator also installs cluster-scoped CRDs and RBAC. Wasm components share a
runtime pod; they are not separate Kubernetes deployments.

Do not run `create.fish` or `destroy.fish` for this comparison: they create or
remove the shared cluster/registry. The commands below run from this directory
unless another working directory is specified. The shell examples use zsh.

## Build prerequisites

Install Docker/BuildKit, Rust, Fish, Python 3.11.8 or newer, curl, patch, k3d,
Helm, kubectl and the **wasmCloud 1.x wash CLI**.
The repo uses wash 0.38.0 with wasmCloud 1.9.2; newer 2.x tools use a different
application model. One way to install the pinned CLI is:

```zsh
rustup toolchain install 1.84.1 --profile minimal
cargo +1.84.1 install wash-cli --version 0.38.0 --locked
rustup target add wasm32-wasip1
wash --version
curl --fail http://localhost:5001/v2/
```

Use Rust 1.84.1 specifically to compile this older wash release. Installing it
with recent Rust versions can fail in `wasmtime-wasi 26.0.1` with an `E0080`
`DirectoryEntry::ALIGN32` assertion. The `+1.84.1` applies only to the CLI
installation; it does not change the default Rust compiler used for this repo's
components. See the [upstream Wasmtime issue](https://github.com/bytecodealliance/wasmtime/issues/10184).

The build configs select their actual WIT worlds and the current Preview 1 Rust
target. `wash build` fetches WIT dependencies, compiles, adapts, and signs the
components. Keep the generated signing keys for subsequent rebuilds.

## Build and push

```zsh
# Build the patched native host and HTTP provider (first build can take many minutes).
sh ../experiments/transport-registration/build.sh
# Build/publish the application components. The registry must already be running.
fish ../build_and_push.fish
fish ./publish-transport.fish
```

This builds and pushes all six signed components, stopping at the first failure.
It pushes to `localhost:5001/v2/platform-poc/<component>:0.1.0` (HTTP controller
uses `0.1.3` for request-body diagnostics and diagnostic endpoints); Kubernetes pulls
those same repository paths through `host.docker.internal:5001`. The patched
HTTP provider is also published by `deploy.fish`. The native host image is imported into every k3d node by `configure-host.fish`. No extra
registry or cluster is needed. PostgreSQL credentials are not needed to build.

## Configure Wasm data

Start the existing host PostgreSQL and Redis services. Create a separate database
and role, using your normal PostgreSQL administrator account:

```zsh
"$(brew --prefix postgresql@15)/bin/createuser" --pwprompt platform-poc-wasm
"$(brew --prefix postgresql@15)/bin/createdb" --owner=platform-poc-wasm platform-poc-wasm
cp .env.secret.example .env.secret
```

Set `POSTGRES_PASSWORD` in `.env.secret` to the password you chose. This file uses
literal `KEY=value` lines, without shell quotes or shell expansion. The username
and database defaults match the commands above. Existing roles/databases need
not be created again.

`wadm.yaml` is a template. `deploy.fish` renders its three PostgreSQL settings to
`.generated/wadm.yaml` with restricted permissions. Both the credentials file
and generated directory are ignored by Git. Do not apply the template directly.
For this local demo the rendered OAM application contains the password, and its
configuration is accessible to users with appropriate cluster/lattice access.

Wasm uses Redis logical database **1**; Java keeps its existing database. The
PostgreSQL and Redis servers themselves remain shared and are excluded from the
Kubernetes resource measurements. Keep using `host.docker.internal`, which works
with this machine's container runtime.

## Install and deploy

```zsh
./install-operator.fish
./deploy.fish
kubectl --context k3d-platform-poc -n wasm get pods,applications,services
```

The installer pins `wasmcloud-platform` chart 0.1.2 and operator 0.4.0, overrides
wadm to 0.19.0, sets both platform NATS addresses to the `wasm` namespace, and creates one
patched wasmCloud 1.9.2 host (`platform-poc/wasmcloud:1.9.2-regack.2`). There is
one NATS server with JetStream, rather than the chart's three-server default. The host is managed by the standalone
`wasmcloud-host.yaml`, not duplicated by Helm's `hostConfig` option.

The host's `natsAddress` must omit the port: this operator appends client port
4222 and leaf-node port 7422. Wadm's `server` setting includes port 4222.

The host is pinned to 1.9.2. [PR #4398](https://github.com/wasmCloud/wasmCloud/pull/4398)
adds a wall-clock timeout around component invocation futures, releasing their
concurrency permits when they stall. Tagged source contains this fix in 1.9.0
and 1.9.2, but not 1.8.0. The test setup sets
`WASMCLOUD_MAX_EXECUTION_TIME_MS=5000` (five seconds per executing invocation).
This does not bound time spent waiting in a queue or guarantee HTTP recovery.

Operator 0.4.0 cannot configure host environment variables through its CRD.
The patched host image therefore carries the five-second execution limit and
four-second RPC timeout as image defaults, preserving them through operator
rollouts. `configure-host.fish` also sets them explicitly on the Deployment,
and imports the host image. `install-operator.fish` calls it automatically. To
reapply the configuration:

```zsh
fish ./configure-host.fish
```

No component rebuild or database reinitialization is required for this host
upgrade. Verify the application is `Deployed` and `./test.fish` passes before
running the load test.


The HTTP controller, orders, and inventory components each allow 10 concurrent
instances on the existing host; other components retain one. These are component
concurrency limits, not extra Kubernetes pods. The HTTP provider uses a
daemonscaler. Wait for the application to show `Deployed` and its HTTP
Service `platform-poc` to appear. Deployment also installs the HTTP ingress;
initialize and test through the persistent Wasm endpoint:

```zsh
curl --fail-with-body --connect-timeout 5 --max-time 20 http://localhost:8080/data-init/all
./test.fish
```

Run data initialization once for a fresh Wasm database. Repeating it inserts
additional inventory rows.

Java uses `localhost:8081/api/...`; Wasm uses `localhost:8080/products`,
`/inventory`, `/orders`, and `/data-init`. Both host ports map persistently to
Traefik on cluster port 80. The Wasm ingress routes to the `platform-poc-http`
Service, whose selector matches the `wasmcloud-host` pods. Kubernetes maintains
its EndpointSlices as pods become ready, move, or are deleted. HTTP needs no
background port-forward process; the cluster and container runtime must remain
running. Since the ports share Traefik, both sets of routes are reachable through
either port; the test scripts use the designated port for each implementation.

The separate selector-based Service avoids stale addresses in operator 0.4.0’s
manual EndpointSlice management. Its [service reconciliation code](https://github.com/wasmCloud/wasmcloud-operator/blob/v0.4.0/src/services.rs#L533)
notes that deleted pods can leave stale IPs. The operator-managed `platform-poc`
Service remains available for the operator, but ingress uses `platform-poc-http`.
This assumes the HTTP provider listens on port 8080 on each selected host, as
configured by this repository’s single-host application. Pod readiness does not
guarantee that the HTTP provider has finished starting; check `/products` after
deployment.

After removing the stale operator endpoint and switching ingress to this Service,
the 2026-10-24 test through `http://localhost:8080` accepted all 30,001 orders at
100 requests/second for five minutes with 250 VUs: zero failures, timeouts, or
dropped iterations. Mean latency was 38.23 ms, p95 127.43 ms, and maximum
1,577.39 ms. Component concurrency remained 10 for the HTTP controller, orders,
and inventory, with the five-second host execution limit and experimental
registration-ack transport unchanged. This verifies the routing correction;
occasional latency spikes still remain.

For an existing deployment, stop the legacy HTTP forward before adding the
persistent mapping, then apply and check the ingress:

```zsh
./down.fish
./up.fish
./test.fish
```

`up.fish` and `deploy.fish` add the 8080 mapping if missing. This uses
[`k3d cluster edit --port-add`](https://k3d.io/v5.1.0/usage/commands/k3d_cluster_edit/),
which recreates the load-balancer container and may briefly interrupt Java and
Kubernetes API access. The cluster and workloads are preserved. New cluster
creation scripts include both mappings already.

`down.fish` stops legacy HTTP forwarding and optional NATS forwarding, leaving
persistent HTTP access available. Use `./up.fish --nats` only when you also need
local control-plane access on ports 14222/14223. These scripts leave the shared
databases and Java's NATS running.

To create an order:

```zsh
curl --fail-with-body --connect-timeout 5 --max-time 20 \
  http://localhost:8080/orders \
  -H 'Content-Type: application/json' \
  -d '[{"sku":"ENG-V8-500","price":8500,"quantity":1}]'
```

Run `./benchmark.fish` from this directory for 10,000 order requests with
concurrency 100 through the Kubernetes ingress (requires `oha`). The root
benchmark script still defaults to port 8080 for standalone local deployments;
the Kubernetes wrapper sets `PLATFORM_POC_ENDPOINT=http://localhost:8080`.

For the Java/Wasm comparison, the repository-root `orders_wasm.js` runs k6 at
100 order requests per second for five minutes with 250 preallocated VUs:

```zsh
k6 run --summary-export=/tmp/wasm-orders-summary.json ../../orders_wasm.js
```

It counts responses by status, client timeouts, and transport failures, and logs
one failure sample per VU at most (including a truncated response body). Its
`order_failures` threshold makes any unsuccessful order fail the test, and
`dropped_iterations` fails it if k6 cannot maintain the scheduled load. Set
`BASE_URL` to test a direct provider connection; the default is
`http://localhost:8080`.
Both `orders_java.js` and `orders_wasm.js` default to 250 k6 VUs while retaining
100 requests/second; set `ORDER_VUS` to override the allocation and ceiling. There
are no retries. Set `TEST_DURATION=2m` for a shorter diagnostic run; the default
remains five minutes. The HTTP client timeout remains 60 seconds so this test can
distinguish client timeouts from the host's five-second execution limit.

The HTTP controller logs `ORDER_JSON_INVALID` with the received byte count and
parse location, without logging the request payload. It logs
`ORDER_BODY_READ_FAILED` and returns 500 if reading the request stream fails.
Inspect those diagnostics alongside RPC timeout messages:

```zsh
kubectl --context k3d-platform-poc -n wasm logs deployment/wasmcloud-host \
  -c wasmcloud-host --since=10m | \
  rg 'ORDER_JSON_INVALID|ORDER_BODY_READ_FAILED|component invocation timed out|data receipt timed out|data transmission timed out'
```

Diagnostic runs at 100 requests per second reproduced 400 responses with
`bytes=0 category=Eof`: the controller received an empty body despite the
load test sending valid JSON. RPC receipt/transmission deadlines were also
observed. Testing HTTP provider 0.27.0 did not eliminate either failure; the
deployment retains 0.25.0. These diagnostics locate the symptoms but do not yet
identify the underlying transport defect.

### Check request-body transport

`POST /body-check` reads the raw request body and returns
`{"bytes":<received byte count>,"sha256":"<lowercase SHA-256 hex>"}` with status
200. It does not parse JSON, invoke other components, or access the database,
Redis or messaging providers. Empty bodies also return 200 with zero bytes and
the empty-body hash, allowing the caller to detect unexpected body loss. Stream
read errors return 500 and log `BODY_CHECK_READ_FAILED`; other methods return 405.

After building/pushing the updated controller and running `./deploy.fish`:

```zsh
curl --fail-with-body http://localhost:8080/body-check \
  -H 'Content-Type: application/json' \
  --data-binary '[{"sku":"ENG-V8-500","price":8500,"quantity":1}]'
k6 run -e BASE_URL=http://localhost:8080 ../../body_check_wasm.js
```

The body-check script sends the same payload as `orders_wasm.js` at 100 requests
per second for two minutes and checks the status, byte count and SHA-256 on every
response. Any mismatch, HTTP failure or dropped iteration fails the test.
Use `RATE` and `TEST_DURATION` to change the load. `BASE_URL` is supported by this
script so the same test can target a direct provider port-forward later.

`POST /constant-response` returns status 200 and the literal body `200 OK`
without reading or parsing the request body or calling downstream services.
Other methods return 405. The normal HTTP routing and request logging still run;
the runtime still handles request resources when the handler returns.

After building/pushing and deploying the updated controller, compare it with
`/body-check` using the same direct-provider port-forward:

```zsh
# In another terminal; wait for the forwarding message before testing.
kubectl --context k3d-platform-poc -n wasm \
  port-forward deployment/wasmcloud-host 18080:8080
```

```zsh
k6 run -e BASE_URL=http://localhost:18080 ../../constant_response_wasm.js
k6 run -e BASE_URL=http://localhost:18080 ../../body_check_wasm.js
```

The constant-response script sends the same JSON payload at the same default
load as the body-check script and validates status 200 and the exact response
body. It also supports `RATE` and `TEST_DURATION`.

To isolate empty POSTs, run the same constant-response test with
`k6 run -e BASE_URL=http://localhost:18080 -e EMPTY_BODY=true ../../constant_response_wasm.js`.
The constant-response test allows 65 seconds of graceful stop so late requests
can reach the default 60-second client timeout instead of being interrupted.

For correlation, set `TRACE_REQUESTS=true` and a unique `RUN_ID`. The script adds
`request_id` to the query string (included in the controller's existing request
log) and logs start/end events with HTTP timings for every request. This adds
diagnostic overhead and is intended for tracing, not latency comparison.

An empty-POST trace on host 1.9.2 with HTTP provider 0.25.0 reproduced requests
that entered the component and produced a successful WASI response but failed
to finish the outer wRPC invocation. Clients waited for their 60-second timeout.
The host's five-second limit wraps the invocation future after acquiring an
execution permit; cancellation logs an error and releases the permit without
constructing an HTTP error response. It also does not cover permit acquisition.
The provider separately wraps its RPC wait only when the link's `timeout_ms`
setting is present; this deployment currently leaves it unset. A provider
deadline would bound that wait, but would not repair lost RPC completion.

See the pinned [host timeout path](https://github.com/wasmCloud/wasmCloud/blob/v1.9.2/crates/host/src/wasmbus/mod.rs),
[WASI response handling](https://github.com/wasmCloud/wasmCloud/blob/v1.9.2/crates/runtime/src/component/http.rs)
and [HTTP provider 0.25.0 timeout path](https://github.com/wasmCloud/wasmCloud/blob/v1.5.1/crates/provider-http-server/src/lib.rs).

### Build the isolated transport experiment

`../experiments/transport-registration` contains separate patches and build
scripts for the subscription-registration acknowledgement experiment:

| Artifact | Pinned release source | Transport | async-nats feature |
| --- | --- | --- | --- |
| Host 1.9.2 | wasmCloud v1.9.2 | wrpc-transport-nats 0.29.0 | async-nats-0_39 |
| HTTP provider 0.25.0 | wasmCloud v1.5.1 | wrpc-transport-nats 0.27.1 | async-nats-0_36 |

Both transport versions enqueue local subscription registration without waiting
for the dispatcher to install the routes. Each patch adds a one-shot
acknowledgement after installation. The caller waits before publishing its
handshake; the callee waits before publishing its handshake response. Other
transport and HTTP interface versions remain pinned to their release lockfiles.

From the repository root, using an ARM64 or AMD64 Docker engine:

```zsh
cd platform-wasmcloud/experiments/transport-registration
sh build.sh
python3 test.py --control
```

The build verifies upstream archive checksums, applies local Cargo overrides,
and builds with `--locked`. It produces the local Docker image
`platform-poc/wasmcloud:1.9.2-regack.2` and signed provider archive
`.build/artifacts/http-server-0.25.0-regack.1.par.gz`. The build selects the
native Docker architecture for both artifacts; it does not build a multi-architecture provider archive. ARM64 is integration-tested;
the AMD64 build path is provided but has not been integration-tested here.
The archive uses separate experiment signing keys under `.build/keys`. Generated sources and artifacts
are ignored by Git; source checksums are recorded in `.build/provenance.json`.
Artifact IDs and checksums are recorded in `.build/artifacts/build.json`.

The provider uses Rust 1.87.0 and the newer host dependency tree uses Rust 1.95.0.
Both Linux builds disable release LTO and use 16 codegen units to reduce build
memory. Use matching build settings for any later unpatched binary comparison.
Only the `wasmcloud` and `provider-http-server` features are selected for their
respective binaries; default provider bundles are not built.

The transport test performs 3,000 echo round trips at 100 concurrent callers
for each version against a disposable NATS server, with a 30-second test
deadline. `--control` also tests the unpatched versions. This raw transport
test is a compatibility check; passing it does not establish that the patch
fixes the HTTP reproducer. The test server is removed afterward. These scripts
do not publish artifacts, change Kubernetes manifests, or deploy the experiment.

The local test on 2026-10-24 deployed both patched artifacts with the existing
component counts, 100 k6 VUs, 100 requests/second and five-second host execution
limit. Empty POST and body-check each completed 12,001 requests with zero
failures or dropped iterations. The final five-minute orders run completed
29,981 successful requests with zero failures, but dropped 19 scheduled
iterations, so it failed the full-load gate. The initial orders run similarly
had zero failures and 34 drops. A separate repeat interrupted by macOS sleep
was excluded. Neither valid log capture recorded runtime warnings or errors.
These historical runs used only 100 k6 VUs. Subsequent tests with 250 VUs
completed 30,001 orders with zero failures or dropped iterations through the
persistent ingress after correcting its stale backend routing.

Results are retained under
`../experiments/transport-registration/.build/results/gated-xrnog3fu/report.json`.
The checked-in Kubernetes defaults now select the patched host and provider.
Host image revision `regack.2` uses the same transport patches as `regack.1`
and additionally carries the execution/RPC timeouts as image defaults. The host
uses an explicit `WasmCloudHostConfig.spec.image`, which takes precedence over
`version`. Raw local result files are ignored by Git and are not shipped with a
fresh clone; rerun the benchmarks to generate measurements on your machine.
The temporary port-forwards, log followers and sleep inhibitor were removed.

## Inspect and remove

```zsh
kubectl --context k3d-platform-poc top pods -n java
kubectl --context k3d-platform-poc top pods -n wasm --containers
./down.fish
./undeploy.fish
```

`undeploy.fish` removes the ingress and OAM application, leaving the platform running.
To remove the platform as well:

```zsh
kubectl --context k3d-platform-poc -n wasm delete wasmcloudhostconfig wasmcloud-host
helm uninstall wasmcloud-platform --kube-context k3d-platform-poc -n wasm
```

The shared cluster/registry, host databases, and Java namespace are preserved.
Helm removal may leave cluster-scoped resources and persistent JetStream volumes.

For the comparison, load one implementation at a time. Namespace separation
does not reserve hardware; all k3d nodes share the same container-runtime VM.
Measure Wasm host startup separately from starting components on an already
running host. Per-component instances represent concurrency within the runtime,
not Kubernetes pod replicas. Existing Java inventory still uses `create-drop`
and seeds at startup; change that before testing multiple inventory replicas.
