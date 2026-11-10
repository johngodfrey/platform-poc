# Platform Proof of Concept

_Comparison of different deployment architectures for a small microservices
application._

1. Java Spring Boot, Containers, Kubernetes
   [example](./java-containers-k8s/README.md)
1. Rust, Containers, Kubernetes [example](./rust-containers-k8s/README.md)
1. Rust, WebAssembly (Wasm) Components [example](./wasm-components/README.md)
   - in wasmCloud on VMs [example](./platform-wasmcloud/README.md)
   - in wasmCloud on Kubernetes, with the wasmCloud Operator
     [example](./platform-wasmcloud/k3d/README.md)
   - in Spin on VMs — to be added
   - in Spin on Kubernetes, with SpinKube — to be added

<!-- prettier-ignore -->
> [!CAUTION]
> This PoC is intended to represent a small "enterprise-like"
> microservices application for demonstration only — it has some small "business
> logic" services, a KV Store, a SQL DB and a message broker. It is not intended
> to be a good example of such an application, and there has been no attention
> paid to best practices, security, or performance.

This diagram shows the application services as Wasm components running in
wasmCloud:

![Platform POC](./platform-wasmcloud/platform-poc.webp)

## Reproduce the Java/Wasm comparison from a fresh clone

These instructions bring up **both applications, their dependencies, the patched
wasmCloud runtime/provider, and the live monitoring dashboard**. Run the shell
commands in **zsh from the repository root**; invoke Fish scripts with `fish` as
shown. Use a terminal where each preceding step has succeeded before continuing.

The validated setup is macOS with an ARM64 Docker engine (OrbStack), k3d 5.9.0,
K3s image `rancher/k3s:v1.36.4-k3s1`, JDK 17, wash 0.38.0 and k6 2.3.0. The native build also
accepts AMD64, but that path has not been integration-tested here. Other Docker
runtimes must support `host.docker.internal` from the k3d nodes. Linux users
must arrange that hostname and host-service connectivity themselves; the
Homebrew service commands below are specific to macOS. Results depend on the
machine and available resources.

Setup order:

1. [Install the tools](#1-install-the-tools).
2. [Create the registry and cluster](#2-create-the-registry-and-cluster).
3. [Start host dependencies and configure credentials](#3-start-host-dependencies-and-configure-credentials).
4. [Build and deploy Java](#4-build-and-deploy-java).
5. [Build and deploy the patched Wasm stack](#5-build-and-deploy-the-patched-wasm-stack).
6. [Initialize and verify the applications](#6-initialize-and-verify-the-applications).
7. [Install monitoring and run the tests](#7-install-monitoring-and-run-the-tests).

### 1. Install the tools

Clone the branch containing this setup (currently `java-wasm-comparison`),
replacing the quoted repository URL:

```zsh
git clone --branch java-wasm-comparison '<repository-url>' platform-poc
cd platform-poc
```

Start OrbStack or your Docker runtime. Install Apple's Command Line Tools if
needed (`xcode-select --install`), [Rust/rustup](https://rustup.rs/), and the
following Homebrew dependencies:

```zsh
brew install fish k3d kubectl helm python@3.11 maven openjdk@17 \
  postgresql@15 redis nats-server k6

export JAVA_HOME="$(brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home"
export PATH="$HOME/.cargo/bin:$(brew --prefix python@3.11)/libexec/bin:$(brew --prefix openjdk@17)/bin:$PATH"

docker info
java -version
mvn -version
python3 --version
k3d version
k6 version
```

Use Python **3.11.8 or newer**. The component build below selects Rust 1.99.0.
Install the Wasm target for it and build the pinned wasmCloud 1.x
CLI using the older compiler needed by that CLI's Wasmtime dependencies:

```zsh
rustup toolchain install 1.99.0 --profile minimal
rustup target add --toolchain 1.99.0 wasm32-wasip1
rustup toolchain install 1.84.1 --profile minimal
cargo +1.84.1 install wash-cli --version 0.38.0 --locked
wash --version
```

Use **wash 0.38.0**, not wasmCloud 2.x tooling. Installing wash with a newer Rust
compiler can trigger the `DirectoryEntry ... ALIGN32` compile failure. The
native patched host/provider builds use their own pinned Rust compilers inside
Docker; you do not need to change your default Rust compiler for those builds.
Allow substantial disk space for Rust/Docker build caches and time for the
first native build. Avoid compiling images during resource measurements.

### 2. Create the registry and cluster

Add this entry to `/etc/hosts` with your preferred administrator editor:

```text
127.0.0.1 k3d-platform-poc.localhost
```

Create the shared cluster **once**. Skip creation if it already exists; use the
[restart instructions](#restart-or-remove-the-local-setup) instead.

```zsh
fish java-containers-k8s/kubernetes/local_k3d/create.fish
kubectl --context k3d-platform-poc get nodes
curl --fail http://localhost:5001/v2/
```

This creates one server and two agents using the pinned K3s image, plus the
local registry. Traefik, Metrics Server and local-path storage come from K3s.
Wait until all three nodes are `Ready`.

| Purpose | Address / namespace |
| --- | --- |
| Kubernetes context | `k3d-platform-poc` |
| Kubernetes API | Local port `6550` |
| Local image/artifact registry | Host port `5001` |
| Java application | `http://localhost:8081/api/...`, namespace `java` |
| Wasm application | `http://localhost:8080/...`, namespace `wasm` |
| Grafana / Prometheus | `http://localhost:8080/grafana/` and `/prometheus/`, namespace `monitoring` |

Both application ports map to the same Traefik ingress on cluster port 80;
paths select the implementation. They are designated test URLs rather than
exclusive listeners for each application. No HTTP port forwarding is needed.

### 3. Start host dependencies and configure credentials

PostgreSQL 15 and Redis run on the host and are shared by the implementations.
Java also uses a host NATS server; Wasm gets its own NATS inside Kubernetes.

```zsh
fish java-containers-k8s/kubernetes/local_k3d/up.fish
brew services list
```

For a **new database installation**, create these roles and databases using your
PostgreSQL administrator account. Each `createuser` prompts for a password;
record it for the credential file in the next step. Skip roles/databases that
already exist. Add `-U YOUR_ADMIN_ROLE` if your administrator is not the default
local account.

```zsh
PG_BIN="$(brew --prefix postgresql@15)/bin"
"$PG_BIN/createuser" --pwprompt inventory-service
"$PG_BIN/createdb" --owner=inventory-service inventory-service
"$PG_BIN/createuser" --pwprompt order-service
"$PG_BIN/createdb" --owner=order-service order-service
"$PG_BIN/createuser" --pwprompt platform-poc-wasm
"$PG_BIN/createdb" --owner=platform-poc-wasm platform-poc-wasm
```

The services must accept connections from the container runtime via
`host.docker.internal`: PostgreSQL on 5432, Redis on 6379 and Java's NATS on
4222. Configure PostgreSQL's listening interface and `pg_hba.conf` for your
runtime network if required. Do not solve connection failures by opening the
services to arbitrary networks or disabling authentication. This optional
check prompts for the inventory role's password and verifies a connection from
Docker before deploying the applications:

```zsh
docker run --rm -it postgres:15 psql -h host.docker.internal \
  -U inventory-service -d inventory-service -W -c 'SELECT 1;'
```

Create credential files only if they do not already exist:

```zsh
test -f java-containers-k8s/kubernetes/manifests/overlays/local/.env.secret || \
  cp java-containers-k8s/kubernetes/manifests/overlays/local/.env.secret.example \
     java-containers-k8s/kubernetes/manifests/overlays/local/.env.secret
test -f platform-wasmcloud/k3d/.env.secret || \
  cp platform-wasmcloud/k3d/.env.secret.example platform-wasmcloud/k3d/.env.secret
chmod 600 java-containers-k8s/kubernetes/manifests/overlays/local/.env.secret \
  platform-wasmcloud/k3d/.env.secret
```

Edit them to match your databases; do not leave example passwords or
`CHANGE_ME` in place. Values are literal `KEY=value`, without shell quotes.

| File | Required configuration |
| --- | --- |
| Java local overlay `.env.secret` | Inventory/order database URLs, role names and passwords; `nats_url=host.docker.internal:4222`; Redis host `host.docker.internal` and port `6379` |
| Wasm `k3d/.env.secret` | `POSTGRES_USERNAME=platform-poc-wasm`, `POSTGRES_DATABASE=platform-poc-wasm`, and the password you chose |

Java uses its existing/default Redis database; Wasm uses logical database 1.
Real credentials, generated manifests, signing keys and local build/results
folders are ignored by Git. Keep existing signing keys for subsequent builds.

### 4. Build and deploy Java

```zsh
fish java-containers-k8s/kubernetes/local_k3d/build.fish
fish java-containers-k8s/kubernetes/local_k3d/deploy.fish

for service_name in inventory-service order-service product-service notification-service; do
  kubectl --context k3d-platform-poc -n java rollout status \
    "deployment/$service_name" --timeout=180s || break
done
kubectl --context k3d-platform-poc -n java get pods,services,ingresses
```

Maven/Jib publishes the four service images to the local registry. Kustomize
creates the `java` namespace, generated Secret, Deployments, Services and
Ingresses. The local order service uses the Java namespace's inventory service.
Inventory seeds its demo records at startup; its `create-drop` setting means
pod restarts recreate those demo tables.
The local overlay uses **Recreate**, so the old inventory JVM finishes dropping
its tables before the replacement creates/seeds them. RollingUpdate can let
the old pod drop tables after the new pod is ready, causing HTTP 500s despite
healthy pod/readiness status. Keep this demo deployment at one replica.

### 5. Build and deploy the patched Wasm stack

Build **both** patched transports, then compile and publish the six components:

```zsh
caffeinate -i -s sh platform-wasmcloud/experiments/transport-registration/build.sh
RUSTUP_TOOLCHAIN=1.99.0 fish platform-wasmcloud/build_and_push.fish
```

On platforms without `caffeinate`, invoke the build with `sh` and use your
platform's sleep inhibitor. Builds verify source SHA-256 checksums and use
locked Cargo dependencies. Outputs include:

- Native host image: `platform-poc/wasmcloud:1.9.2-regack.2`.
- Signed HTTP provider archive:
  `platform-wasmcloud/experiments/transport-registration/.build/artifacts/http-server-0.25.0-regack.1.par.gz`.
- Source provenance and image/binary/patch checksums under `.build/`.

These artifacts are built locally, not downloaded from a registry owned by the
original developer. The build chooses the Docker engine's native architecture;
it does not produce a multi-architecture provider archive. Keep `.build/keys`
and the components' signing keys to preserve artifact identities when rebuilding.

Install the pinned platform and deploy the application:

```zsh
fish platform-wasmcloud/k3d/install-operator.fish
fish platform-wasmcloud/k3d/deploy.fish
kubectl --context k3d-platform-poc -n wasm get pods,applications,services
```

The installer uses platform chart **0.1.2**, operator **0.4.0** and wadm
**0.19.0**, starts a single NATS server with JetStream, imports the patched host
image into **every k3d node**, and applies the host configuration/timeouts.
Deployment renders the PostgreSQL credentials into a private generated manifest,
pushes the patched provider to the local registry and applies the application,
selector-based HTTP Service and ingress. Components share one host pod; they
are not separate Kubernetes Deployments. Use these `k3d` scripts for the
comparison; `platform-wasmcloud/local` runs a different host-local setup.

Wait for the Wasm host rollout, then watch until the application reports
`Deployed` (Ctrl-C stops the watch):

```zsh
kubectl --context k3d-platform-poc -n wasm rollout status \
  deployment/wasmcloud-host --timeout=180s
kubectl --context k3d-platform-poc -n wasm get applications --watch
```

### 6. Initialize and verify the applications

First verify that Wasm HTTP is serving requests:

```zsh
curl --fail-with-body --max-time 20 http://localhost:8080/products
```

For a **new Wasm database only**, initialize the demo data once:

```zsh
curl --fail-with-body --max-time 60 http://localhost:8080/data-init/all
```

Do not repeat initialization after a cluster restart or an ordinary redeploy:
it inserts additional inventory rows into the existing host database. If
initialization fails after partial progress, inspect the logs/data before
retrying blindly.

Run the functional checks and explicitly verify both order paths:

```zsh
fish java-containers-k8s/kubernetes/local_k3d/test.fish
fish platform-wasmcloud/k3d/test.fish

curl --fail-with-body --max-time 20 http://localhost:8081/api/order \
  -H 'Content-Type: application/json' \
  --data '{"orderLineItemsDtoList":[{"skuCode":"iphone_13","price":200,"quantity":1}]}' \
  -w '\nHTTP %{http_code}\n'
curl --fail-with-body --max-time 20 http://localhost:8080/orders \
  -H 'Content-Type: application/json' \
  --data '[{"sku":"ENG-V8-500","price":8500,"quantity":1}]' \
  -w '\nHTTP %{http_code}\n'
```

Both POSTs should return **201**. Inspect HTTP statuses in the Java functional
script: its verbose curl commands do not fail the script on HTTP errors.

### 7. Install monitoring and run the tests

Install the complete provisioned monitoring stack before running either Python
demo runner:

```zsh
python3 monitoring/footprint/install.py
kubectl --context k3d-platform-poc -n monitoring get pods
```

Open [Grafana](http://localhost:8080/grafana/d/java-wasm-footprint). Anonymous
Viewer access is enabled for this local demo. Prometheus is available at
[localhost:8080/prometheus/](http://localhost:8080/prometheus/). The installer
waits for both Deployments to roll out and persists their data in local PVCs.
Grafana's corrected memory/CPU/database settings are included; no manual import,
credentials from another machine or port-forward process is needed.

Choose the test that answers your question; run Java and Wasm sequentially:

| Test | Command from the repository root | What to watch |
| --- | --- | --- |
| Baseline throughput/latency | `caffeinate -i -s k6 run orders_java.js`, then `caffeinate -i -s k6 run orders_wasm.js` | Terminal summaries: 100 orders/s for five minutes, 250 VUs each |
| Live footprint | `python3 monitoring/footprint/run.py both` | Grafana: 30s idle → 120s at 100 orders/s → 30s recovery for each implementation |
| Fixed-budget capacity | `python3 monitoring/budget/run.py plan`, then `python3 monitoring/budget/run.py run both` | Grafana plus HTML/CSV results: 2 CPU cores and 4 GiB RAM per implementation, increasing offered load |

The Python runners inhibit sleep on macOS and stream their metrics into
Prometheus. **The standalone `orders_java.js` / `orders_wasm.js` commands do
not stream to Grafana**; use the Python runners for the provisioned live test
panels. The standalone Java script uses port 8081 directly, whereas the Wasm
script accepts `BASE_URL` and defaults to port 8080. Check failures, acceptance
checks and dropped work in both summaries; Java's standalone script does not
have the same failure thresholds as the Wasm script. Keep the same monitoring
setup active for both implementations when comparing results.

The capacity test restarts pods when applying/restoring resource limits and
recreates Java's demo inventory tables. Read its detailed section below before
running it. Keep host services running, close unrelated load tests and prevent
machine sleep throughout measurements. A sleep-interrupted run is invalid.

## Wasm reliability fixes included in this setup

The default Kubernetes manifests select the patched artifacts. Replacing them
with the stock upstream images would remove the transport fix.

| Fix / setting | Committed implementation and effect |
| --- | --- |
| Host-side subscription registration | [Host transport patch](platform-wasmcloud/experiments/transport-registration/patches/wrpc-transport-nats-0.29.0.patch): await local subscriber registration before publishing the handshake or its response. Host source is wasmCloud 1.9.2. |
| Provider-side subscription registration | [Provider transport patch](platform-wasmcloud/experiments/transport-registration/patches/wrpc-transport-nats-0.27.1.patch): apply the same acknowledgement on the HTTP provider's separate transport version. Provider source is wasmCloud 1.5.1 / HTTP provider 0.25.0. Both sides must be patched. |
| Bounded component execution | [Patched host image](platform-wasmcloud/experiments/transport-registration/Dockerfile) carries `WASMCLOUD_MAX_EXECUTION_TIME_MS=5000` and `WASMCLOUD_RPC_TIMEOUT_MS=4000`; [host setup](platform-wasmcloud/k3d/configure-host.fish) also sets them explicitly. Image defaults survive operator-created Deployment replacements. Host 1.9.2 includes the upstream invocation-timeout fix. |
| Correct artifact selection | [Host manifest](platform-wasmcloud/k3d/wasmcloud-host.yaml) sets `spec.image` to `1.9.2-regack.2` (overrides `version`). [Application manifest](platform-wasmcloud/k3d/wadm.yaml) selects the patched provider `0.25.0-regack.1` in the local registry. [Deployment](platform-wasmcloud/k3d/deploy.fish) publishes it automatically. |
| Persistent, current HTTP backends | [Selector-based Service](platform-wasmcloud/k3d/http-service.yaml) and [ingress](platform-wasmcloud/k3d/ingress.yaml) use `platform-poc-http`, whose endpoints Kubernetes updates with pod readiness/IP changes. This avoids stale addresses retained in operator 0.4.0's manually managed `platform-poc` EndpointSlice. |
| Consistent component concurrency | The application sets HTTP controller, orders and inventory concurrency to **10** each; other components retain one. This is component concurrency, not ten pods. Keep it unchanged when comparing test runs. |
| Failure instrumentation and isolated diagnostics | The built HTTP controller is version **0.1.3**, with constant-response and body-check endpoints plus request/body error logging. The Wasm order test counts response statuses, transport failures, timeouts and dropped work, and samples failed responses. These diagnostics remain available in a fresh deployment. |
| Enough load-generator workers | Both order benchmarks default to **250 VUs** while retaining 100 orders/s. This prevents a 100-VU ceiling from dropping scheduled requests during brief latency spikes; it does not accelerate the application. |
| Reliable monitoring and interpretation | Grafana has a 1 GiB memory limit, bounded CPU/Go memory, SQLite WAL and longer health-probe timeouts. CPU cost uses matching 30-second windows, separate Run IDs and steady-load-only data; recovery-phase near-zero throughput does not create misleading peaks. |

The execution/RPC settings are not an end-to-end HTTP deadline: queueing and
other transport work can take additional time. The patched provider/host use
locked dependencies and checksum-pinned source archives, not an unpinned
upstream transport upgrade. The native builds disable release LTO and use 16
codegen units; those settings are part of this benchmark configuration.

To confirm the deployed artifact references and runtime limits:

```zsh
kubectl --context k3d-platform-poc -n wasm get deployment wasmcloud-host \
  -o 'jsonpath={.spec.template.spec.containers[?(@.name=="wasmcloud-host")].image}{"\n"}'
kubectl --context k3d-platform-poc -n wasm get applications platform-poc \
  -o 'jsonpath={.spec.components[?(@.name=="http-server")].properties.image}{"\n"}'
kubectl --context k3d-platform-poc -n wasm exec deployment/wasmcloud-host \
  -c wasmcloud-host -- sh -c \
  'printf "execution_ms=%s rpc_ms=%s\n" "$WASMCLOUD_MAX_EXECUTION_TIME_MS" "$WASMCLOUD_RPC_TIMEOUT_MS"'
kubectl --context k3d-platform-poc -n wasm get endpointslices \
  -l kubernetes.io/service-name=platform-poc-http -o wide
```

Expect host `platform-poc/wasmcloud:1.9.2-regack.2`, provider
`host.docker.internal:5001/v2/platform-poc/http-server:0.25.0-regack.1`, execution
limit `5000` and RPC timeout `4000`. The HTTP endpoints should correspond to the
current host pod IP. The legacy operator-managed Service can still exist;
ingress deliberately uses the selector-based Service instead.

Optional diagnostics isolate transport/body handling without database work:

```zsh
curl --fail-with-body --max-time 10 -X POST --data '' \
  http://localhost:8080/constant-response
curl --fail-with-body --max-time 10 http://localhost:8080/body-check \
  -H 'Content-Type: application/json' \
  --data '[{"sku":"ENG-V8-500","price":8500,"quantity":1}]'
# Concurrent handshake checks against a disposable NATS container:
python3 platform-wasmcloud/experiments/transport-registration/test.py
```

The constant endpoint returns `200 OK` without reading the body; body-check
returns its byte count and SHA-256. These help distinguish transport failures
from downstream failures. The raw transport tests check compatibility; the
end-to-end HTTP/order benchmarks remain necessary. See the
[Wasm deployment notes](platform-wasmcloud/k3d/README.md) for source/version and
historical diagnostic details.

## Live Java/Wasm footprint demonstration

The local `k3d-platform-poc` setup includes separate footprint, budget, memory-density, capacity and scale-out Grafana dashboards and
Prometheus in a separate `monitoring` namespace. Install or update them from the
repository root while the cluster and its existing ingress are running:

```zsh
python3 monitoring/footprint/install.py
```

Open [Java vs Wasm — live footprint](http://localhost:8080/grafana/d/java-wasm-footprint).
Read-only viewing needs no login. Java is blue and Wasm is green. Both charts
and container metric collection update every five seconds; CPU is averaged
over 30 seconds. No port-forward process is required. Prometheus is available
at [localhost:8080/prometheus/](http://localhost:8080/prometheus/).

With the existing applications running, run the demonstrations sequentially:

```zsh
python3 monitoring/footprint/run.py both
# Or choose either implementation:
python3 monitoring/footprint/run.py java
python3 monitoring/footprint/run.py wasm
```

Each demonstration has 30 seconds idle, 120 seconds at 100 orders/second, and
30 seconds recovery. The runner prevents idle sleep on macOS for the entire
sequence. Both implementations get 250 order workers; one extra worker emits
phase markers without sending HTTP requests. The demo uses the same order
payloads as the repository-root order tests. It does not change their settings,
component concurrency, host images, execution limits, or database contents other
than placing orders. Start with warmed applications; this is not a cold-start
measurement. Do not run other load tests at the same time.

The dashboard shows application memory and CPU, namespace memory and CPU,
accepted versus offered throughput, p95/mean latency, failures, dropped work,
phase markers and a live CPU-seconds-per-1,000-accepted-orders ratio. The
implementation and Run selectors filter load-test results; resource charts
always show both live deployments. The p95/mean latency gauges are cumulative
statistics for each run, not percentiles calculated over the visible time range.
The live CPU-per-order ratio retains each Run ID and uses matching 30-second
CPU/throughput windows. It is shown only after a full window in the load phase;
startup and recovery are excluded. This prevents recovery-phase division by
near-zero throughput from creating misleading peaks. It includes background
application CPU and is not a cumulative cost or an idle-subtracted measurement.
Counter rates use a 20-second window and will smooth the start/end of load.

Application totals include the four Java service containers, or the Wasm host
container including its child provider processes. Namespace totals additionally
include Wasm's NATS sidecar, NATS server, wadm and operator. **Shared host
Postgres/Redis and Java's host-run NATS are excluded**; the namespace chart is
not a complete deployment cost comparison. Memory is measured working set, not
Kubernetes requests/limits. Monitoring overhead has its own chart and is
excluded from application totals. These measurements also exclude host/VM
overhead. Keep the same monitoring setup active for both runs.

Use a 15-minute dashboard window to see the two default runs together. A unique
Run ID separates repeated runs. Missing test data before a run means no metrics
have been received; it does not mean zero requests. Test logs and k6 summary JSON
are saved under `monitoring/footprint/.results/` (ignored by Git). For a short
connectivity check instead of the presentation sequence:

```zsh
python3 monitoring/footprint/run.py both --idle 5 --load 15 --recovery 5
```

The Prometheus configuration, recording rules, datasource and all five dashboards are
generated by `monitoring/footprint/generate.py`; the generated dashboards and
resource JSON are checked in for review. The installer pins Prometheus 3.15.1
and Grafana 13.2.4. It creates a random administrator password in a Kubernetes
Secret and enables anonymous Viewer access for this local demo. It does not
print or store that password in repository files. Prometheus remote write is
enabled for the local k6 runner. Do not expose these demo ingress routes to
untrusted networks. The Prometheus service account can read node metrics through
the Kubernetes API proxy.

Grafana has a 1 GiB memory limit and a one-core CPU limit; its Go runtime uses
at most two processors and a 768 MiB soft memory target. SQLite write-ahead
logging and a single database connection are configured for this local instance.
The original 512 MiB limit caused an `OOMKilled` restart during dashboard use,
followed by readiness failures and ingress `no available server` responses.
Health probes allow five seconds for a response. To inspect a recurrence:

```zsh
kubectl --context k3d-platform-poc -n monitoring get pods
kubectl --context k3d-platform-poc -n monitoring describe pod -l app=footprint-grafana
kubectl --context k3d-platform-poc -n monitoring logs deployment/footprint-grafana --tail=50
```

Monitoring data uses 2 GiB of local Prometheus storage (seven-day retention,
capped at 1,500 MB) and 1 GiB for Grafana. Storage survives pod and cluster
stop/start, but is not a backup against cluster deletion. To stop monitoring
without deleting its data:

```zsh
kubectl --context k3d-platform-poc -n monitoring scale \
  deployment/footprint-prometheus deployment/footprint-grafana --replicas=0
```

Rerun `install.py` to resume. Application deployments are left running.

## Fixed-budget application capacity test

The fixed-budget test measures the highest **tested** order rate meeting a
latency/reliability target with equal total application CPU and RAM limits. It
measures throughput density, not a number of independent application copies.
Run it on the existing local comparison setup with monitoring active.

First inspect the allocations and validate them against Kubernetes without
changing workloads:

```zsh
python3 monitoring/budget/run.py plan
```

The default budget is **2 CPU cores and 4 GiB RAM for each implementation**:

| Application | CPU allocation | Memory allocation |
| --- | --- | --- |
| Java | 500 millicores per service, four services | 1 GiB per service |
| Wasm | 2 cores shared in the host container | 4 GiB shared in the host container |

Requests and limits are set to the same values. This is a sum of container
budgets, not an identically shared resource pool: Java cannot borrow unused CPU
from its other services, whereas the Wasm components share one host budget.
The four-way split can limit Java throughput before its total CPU is used.
Change `monitoring/budget/budget.json` to test another split; allocations must
still sum to the same total for both implementations. Report the allocation
alongside the results and do not attribute a partitioning bottleneck to the
programming language. Shared Postgres/Redis, Java's host NATS, Wasm's sidecar,
NATS/control-plane pods, Kubernetes and monitoring are outside this application
budget. The host machine and databases can still limit observed throughput.

**Applying and restoring limits rolls the application pods. Java inventory's
`create-drop` setting recreates its demo tables on restart. Use disposable demo
data and stop other load tests/deployments first.** To run Java followed by Wasm:

```zsh
python3 monitoring/budget/run.py run both
```

Each implementation gets a 30-second warmup at 10 orders/second. The test then
tries **1, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45 and 50 orders/second**, holding each for 60 seconds, with
250 order workers. A stage passes only if p95 is **below 100 ms**, there are
zero failed requests and zero dropped iterations, and the intended request
count is delivered (allowing one boundary iteration). Each ladder stops at its
first failed rate. If all rates pass, the result is a lower bound on capacity;
it is not proof that the last rate is the maximum. The lower starting rates help
find a passing Java baseline; five-order steps refine the previously observed
Wasm boundary between 25 and 50 orders/second. Budgets, warmup, execution limits,
worker counts and pass criteria stay unchanged. To refine a bracket further
(for example, after 25 passes and 30 fails):

```zsh
python3 monitoring/budget/run.py run both --rates 25,26,27,28,29,30 --seconds 90
```

You can select `java` or `wasm`, change `--p95-ms` or `--warmup-rate`, set the same
`--vus` for both,
or pass `--budget PATH` for a different allocation. Lower throughput from
insufficient load-generator workers still fails the stage; inspect dropped
work and load-generator resources before declaring an application limit.
The benchmark keeps the machine awake on macOS. Default runs can take roughly
32 minutes plus application rollouts if all stages are reached; the other
implementation remains idle. Each implementation stops early at its first failure.

The separate [budget dashboard](http://localhost:8080/grafana/d/java-wasm-budget)
shows each offered-rate step, accepted throughput, latency and application
resources. Its Run selector and load-test charts include only `budget-...` stages,
including warmup; the footprint dashboard excludes them even with All selected.
Both dashboards link to each other and share the same Prometheus installation.
Run `python3 monitoring/footprint/install.py` to install or update both dashboards. Each run/stage
has a unique ID; latency statistics are computed separately for each stage.
The CPU-per-order chart appears only after 30 seconds of steady load.

The runner saves original resource settings **before making changes**, checks
that the budgets stay enforced and pods remain ready without restarts, and
restores the original settings in `finally`, including on a normal Ctrl-C.
It requires the inventory Deployment's Recreate strategy, waits for terminating
pods to disappear, and verifies a real order before warmup and after restoration.
Java's inventory query must report the demo SKU in stock; pod readiness alone
does not detect a missing inventory table. Failed order samples include HTTP
status and a response-body excerpt in the k6 log.
A pod restart, replacement, changed budget, missing request data or failed
warmup invalidates the experiment. Readiness lost under measured load makes that
rate fail; it does not abort the comparison. The runner waits for functional
recovery before testing the other implementation. Failed stages retain recent
application logs before resource restoration. Unexpected concurrent resource edits are
not overwritten. If restoration is interrupted, recover using the state file
printed at startup:

```zsh
python3 monitoring/budget/run.py restore monitoring/budget/.results/RUN/state.json
```

Results are saved under `monitoring/budget/.results/RUN/`: `report.html` is a
shareable results table with the highest passing tested rate for each
implementation; `report.json` includes the budget and outcome; `stages.csv`
supports plotting. Per-stage k6 logs/summaries and container resource samples
are retained too. Partial/error reports do not establish application capacity.
No independent application copies are created, and no data initialization is
performed by this runner.

## Memory-density test with CPU headroom

This experiment measures the **smallest passing tested application memory
allocation** at a fixed order rate, then projects slots in a chosen application
RAM pool. It does **not** deploy multiple independent stacks or prove concurrent
instance density. Validating that projection needs isolated data/routing and a
separate concurrent multi-stack experiment. The projected slots do not account
for their additional CPU demand.

Install/update monitoring, preview the changes, then run:

```zsh
python3 monitoring/footprint/install.py
python3 monitoring/density/run.py plan both
python3 monitoring/density/run.py run both
```

Open the dedicated [memory-density dashboard](http://localhost:8080/grafana/d/java-wasm-memory-density).
It shows live application memory, configured total memory limit, latency,
offered/accepted throughput, failures/drops, application CPU, node CPU utilization
and the **unvalidated projected slots at the current allocation**. All five
benchmark dashboards link to each other and filter their own test runs.
The density dashboard does not turn the current-cap projection into a pass
verdict; use the saved report for validated stages.

The default sweep is **4096, 2048, 1024, 768, 512, 384, 256 MiB** per
application. Java splits each total equally across its four service containers;
Wasm assigns the total to its host container, including child providers. Memory
requests equal limits. This compares equal service allocations against a pooled
host allocation, not optimally tuned Java allocations. **Application CPU limits
are removed**; Java requests 100m per service, Wasm requests 400m for the host.
Other resource settings are preserved. The peer remains idle, and each
implementation is restored before testing the next.

At every allocation, the runner restarts the applications, checks a real order,
warms at **25 orders/s for 120 seconds**, then measures **two 60-second repeats
at 25 orders/s** with 250 order workers. Each repeat must deliver the intended
count, have zero failures/drops/restarts/readiness loss and **p95 < 100 ms**.
Warmup requires reliability and headroom but does not enforce the latency SLO.
All repeats must pass. The runner stops that implementation at the first
non-passing allocation, so the report describes the smallest **passing tested**
allocation rather than an exact minimum. A latency failure alone does not prove
that memory was exhausted. Even the first large allocation can fail the latency
SLO; in that case no passing density is reported.

CPU headroom requires telemetry for every node throughout at least 80% of the
measurement samples and **all sampled 30-second node CPU rates below 60% of
reported cores**. Missing telemetry or a busy node produces
`inconclusive_cpu`, not a memory-capacity failure. The first run after installing
monitoring may need about a minute for enough CPU samples. Node CPU is collected
through the existing Kubernetes API proxy; no extra exporter or public service
is needed. **k3d nodes share a VM**: these checks do not prove physical CPU
isolation or measure macOS pressure, and averages can hide short bursts. Keep
other benchmarks and heavy host work stopped. The runner prevents idle sleep
on macOS for the complete experiment.

The default slot projection uses **8192 MiB reserved for application allocations**:
`floor(8192 / smallest passing tested allocation)`. Reserve additional RAM for
Kubernetes, wasmCloud control plane/sidecar, monitoring, databases, host messaging
and VM/OS overhead. Those services are outside this application-memory budget.
For example, allocating 512 MiB to one application projects 16 slots; it does not
prove 16 applications will pass together or that CPU is sufficient for them.

To refine a boundary after 1024 MiB passes and 768 MiB fails:

```zsh
python3 monitoring/density/run.py run both --limits 1024,960,896,832,768
```

Options include `--rate`, `--seconds`, `--warmup`, `--repeats`, `--p95-ms`,
`--pool-mib` and `--max-node-cpu`. Change the same settings for both applications.
The full default sweep could take roughly an hour plus rollouts if every
allocation is reached; it normally stops sooner. This is a warmed memory-envelope
experiment, not a cold-start timing test. The host memory limit covers startup as
well as load, and lower caps can fail to start at all.

Results, per-repeat k6 summaries/logs, CPU samples and original resource settings
are saved under `monitoring/density/.results/<timestamp>/`. Open `report.html`
for the smallest passing tested allocation and projected slots. Runtime
interruptions/startup failures are recorded as inconclusive rather than silently
counted as memory exhaustion. Resources are restored in `finally`, including on
normal Ctrl-C, and real orders are checked after restoration. Recovery refuses
to overwrite concurrent resource edits or recreated deployments.

**Applying/restoring caps restarts pods and recreates Java inventory's demo
tables. Use disposable demo data and do not deploy or run another benchmark at
the same time.** If the process is killed before restoration, use its saved state:

```zsh
python3 monitoring/density/run.py restore monitoring/density/.results/<timestamp>/state.json
```

## Order-rate saturation comparison

For the equal-budget comparison with a five-minute confirmation of the highest
passing rate, follow [the sustainable-throughput protocol](monitoring/capacity/README.md).
It uses 2 cores and 4 GiB per implementation, a 100 ms p95 limit, two-minute
rate steps and automatic restoration. The original screen below keeps existing
resources and uses a looser default latency limit; keep their results separate.

This test increases offered orders/second while preserving the current
application deployments, replica counts and resource settings. It identifies the
highest **passing tested rate** under a configurable latency/reliability SLO,
not the point at which deliberately prolonged overload crashes the system.
Run it separately from footprint, budget and density experiments:

```zsh
python3 monitoring/footprint/install.py
python3 monitoring/capacity/run.py both
```

Open the [dedicated capacity dashboard](http://localhost:8080/grafana/d/java-wasm-capacity).
It filters `capacity-...` runs; other dashboards exclude them. The runner snapshots
current application resource settings and image references, warms each application
at 100 orders/s for 120 seconds, then tests **100, 200, 400, 800 and 1600 orders/s**
for one minute each. Both use **250 order workers**, a 10-second HTTP request
timeout, and no retries. Idle/recovery markers surround each stage. The runner
keeps macOS awake, runs implementations sequentially and checks a real order
before moving to the peer and at the end. It never changes resources or restarts
pods as part of the test.

A stage passes only with **p95 below 500 ms**, zero failed requests, zero dropped
iterations, the intended request count and unchanged ready pods. Each ladder
stops at its first failure. Dropped work is marked **inconclusive_dropped_work**:
it can reflect insufficient generator workers or generator/host pressure as well
as slow application responses, and does not establish an application maximum.
Pod readiness loss is classified separately. A passing last step is a lower
bound; refine the bracket using the same settings for both implementations:

```zsh
python3 monitoring/capacity/run.py both --rates 200,250,300,350,400 --seconds 120
```

Other options include `--warmup`, `--warmup-rate`, `--p95-ms` and `--vus`.
Unlike the memory test, this experiment does not roll pods, so it measures the
already running applications. Start from comparable healthy deployments and
repeat with the application order reversed by running `wasm` and `java`
separately if investigating order effects.

The runner saves `report.html`, `report.json`, deployment resource/image
snapshots, stage summaries, k6 logs and sampled per-container CPU/memory under
`monitoring/capacity/.results/<timestamp>/`. Use those container samples when
investigating resource use; historical aggregate working-set metrics have shown
inconsistencies after restarts and should not be treated as validated peak-memory
measurements. The report presents highest passing tested rates and per-stage
latency/errors/drops. The complete request path includes shared databases,
messaging, ingress, VM and the local generator, so a bottleneck here is not
necessarily an intrinsic Java or Wasm execution limit. This is a short saturation
screen, not a long-duration production capacity certification.

## Order-tier scale-out under a burst

The default **host mode** creates a real additional Wasm runtime pod (1 → 2),
including its NATS leaf, capability providers and serving components. Java adds
an order-service pod/JVM (1 → 2). These scale different scopes: Java's other
services stay fixed, while Wasm's serving components are distributed over two
hosts. Shared databases, ingress and messaging remain in place. This is manual
scale-out, not an HPA test. A downstream bottleneck may still prevent recovery.

The additional Deployment copies the current host's exact pod template, retaining
the patched runtime/provider configuration, resource settings and execution/RPC
limits. The original operator-managed Deployment and HostConfig are not changed.
A temporary host label and versioned component spreads preserve baseline capacity
and add a second serving set; data-init remains on the original host. Explicit
runtime scale commands supplement wadm reconciliation. The new pod initially
stays outside persistent HTTP routing. It joins only after a unique runtime ID,
matching image digests, providers/components, a direct successful order against
that pod, and a ready EndpointSlice have been verified. A TCP readiness probe checks the HTTP listener; the separate direct order probe
checks application behavior before routing. Images and component artifacts may be cached: this measures
new pod/process startup, not cold image downloads.

Use `--wasm-mode concurrency` to reproduce the previous test that raises only
orders concurrency from 10 → 20 inside the existing host. Host mode currently
supports `--factor 2` (one additional pod).

```zsh
python3 monitoring/footprint/install.py
python3 monitoring/scaleout/run.py plan both --wasm-mode host
python3 monitoring/scaleout/run.py run both --wasm-mode host
```

To repeat the previous 400 orders/s burst while allowing more time for new-host
startup and post-startup observation:

```zsh
python3 monitoring/scaleout/run.py run both --wasm-mode host --burst-rate 400 --after-scale 180
```

Open [Java vs Wasm — scale-out](http://localhost:8080/grafana/d/java-wasm-scaleout).
The dashboard shows order throughput/latency windows, phase markers, the actual
scale-command acknowledgement, Java ready order pods, Wasm verified routable host count, ready host pods, aggregate orders concurrency
and application resources. Capacity series are labelled with the Wasm mode; host
counts and concurrency allowances have different units. Test data is filtered to `scaleout-...`;
other dashboards exclude it. Resource charts are live and not filtered by Run.
Aggregate memory telemetry after restarts remains subject to the inconsistency
noted in the density/saturation sections.

Defaults: **60 seconds warmup + 30 seconds baseline at 100 orders/s**, a one-second
ramp to **200 orders/s**, **45 seconds before scaling**, **120 seconds after the
planned scale time**, then a one-second ramp back and 30 seconds recovery. Both
get 250 order workers and a 10-second HTTP timeout, with no order retries. The
runner prevents idle sleep on macOS, scales implementations sequentially and
restores each before testing the next. The same burst is used even if one
implementation already sustains it; that case must not be presented as a
recovery benefit from scaling.

Both implementations now use an independent two-second monitor for **new-pod
Ready** and a **successful order sent directly to the new pod** through the same
Kubernetes pod-proxy path. The direct check requires HTTP 201 and the expected
application success body, with a ten-second timeout. It bypasses ingress but
exercises downstream calls. These two timings start when the scale request
begins; they are separate from command acknowledgement and full capacity.
Java's capacity check now also requires direct-order success. The native
Kubernetes readiness probes still differ; use the successful direct order as
the common application-readiness measure. Wasm's full-host inventory and routing
checks remain additional measurements, polled independently. Results include
`readiness.json`, probe attempts, new pod UIDs and image digests.

Run five matched pairs, alternating which implementation runs first:

```zsh
python3 monitoring/scaleout/repeat.py --pairs 5 --burst-rate 400 --after-scale 180
```

Allow approximately 65–75 minutes for five pairs. The runner prevents idle sleep,
restores each workload, pins the benchmark source, and stops if a pair fails or
restoration fails. Keep the laptop connected to mains with the lid open and avoid other heavy
work during the series. On macOS, power is recorded before and after each pair;
the runner stops if mains is absent. Sleep prevention remains active between
pairs as well as during each test. Each implementation gets the same 60-second warmup,
30-second baseline at 100/s, 45-second pre-scale burst, 180-second post-scale
period, 30-second cooldown, 250 VUs and ten-second request timeout. No database
reset occurs between trials; cache state and order-data growth can still affect
results. Images may already be cached.

The combined `monitoring/scaleout/.results/repeated-<timestamp>/report.html`
refreshes while the series runs. It shows every result, direct-order readiness
points and medians/ranges across valid matched pairs. Individual pair reports
and raw summaries remain alongside it. Invalid pairs are retained and excluded
from aggregates as whole pairs; they are never silently replaced. Median run
p95s are summaries of run p95s, not pooled request percentiles. Java still scales
only its order tier while Wasm adds a serving host, so scope differs. A run
already healthy before scaling cannot establish a recovery benefit.

Analyse before/after performance from an existing completed series without
running more load:

```zsh
python3 monitoring/scaleout/analyse_phases.py monitoring/scaleout/.results/repeated-<timestamp>
```

Open that series' `phase-analysis/report.html` for the phase comparison, every
pair and a cohort heatmap. JSON and CSV exports preserve the calculations.
The analysis uses common full request-start cohorts before any scale request
and after both resources were observed ready in every trial. Phase mean
latencies are weighted by request count; cohort p95s remain separate and are
never presented as pooled phase percentiles. A temporal before/after change
alone does not establish a causal benefit from scaling.

The report records three separate observations:

- **Command acceptance:** the update returned successfully; it does not prove
  capacity is available.
- **Observed capacity:** Kubernetes reports ready Java order pods with successful direct orders, or `wash get
  inventory` plus direct-pod/endpoint checks verify a second routable Wasm host
  (or the target orders `max_instances` in concurrency mode). Capacity readiness
  does not by itself prove increased end-to-end throughput.
- **Traffic recovery:** two consecutive complete **15-second request-start cohorts**
  after both the actual command acknowledgement and observed target capacity meet **p95 < 500 ms**, have zero
  failed orders in each cohort and enough completed/accepted requests across
  the combined 30-second pair to match the offered burst rate (allowing one
  boundary request per pair). This measures delivery
  during those windows; it does not prove zero scheduled drops in each window.

An application that already passed the last two full burst windows before the
command is labelled **already healthy** and gets no claimed recovery time.
The reported recovery delay is the end of the second qualifying window minus
command acknowledgement, so it includes the observation window and is not an
instantaneous startup latency. Capacity is polled approximately every two
seconds plus API/CLI time; these are observed timings, not subsecond guarantees.
No recovery observed is a valid outcome, especially with fixed downstream tiers.
Inspect `report.json` for all burst windows, accepted counts, p95 and errors, and
the run-wide dropped-iteration count.

Results live under `monitoring/scaleout/.results/<timestamp>/`: `report.html`,
`report.json`, non-secret scale/version backup `state.json`, per-implementation
k6 summaries/logs and time-stamped capacity observations. Recovery claims also require the target
capacity to stay observed after readiness; the report retains polling errors. Control polling uses a
temporary read-only HTTP endpoint bound to localhost. Its requests are excluded
from the custom order latency and throughput metrics. Wasm inventory queries use
a temporary forward of `wasm/nats-0` on localhost:14223; it is stopped afterward
and does not use Java's host NATS. Override `--nats-port` if needed.

The pinned operator 0.4 Application API implements merge patches and ignores
server-side dry-run semantics. The runner therefore uses a real server dry run
for Java and the additional host Deployment; Wasm's Application plan validates a private temporary manifest locally using
`wash app validate`. Actual Wasm scaling uses a unique wadm version via
`wash app deploy` and an explicit `wash scale component` command that preserves
existing component annotations. A declarative update alone reported Deployed
without changing runtime concurrency in the initial probe, so this driver does
not measure autonomous wadm reconciliation latency. Restoration redeploys the
original version, explicitly resets runtime concurrency and removes only the
temporary experiment version. Existing inline provider configuration stays
out of the results and is never printed; temporary files are mode 0600 and
removed. The aggregated Application UID changes with its version, which the
runner accounts for. Expected counts/version checks refuse concurrent edits;
the Wasm version API does not provide a transactional Kubernetes resourceVersion
compare-and-swap, so do not deploy concurrently.

Java order-service must already use Hibernate `update` (as in the local overlay)
and have one replica. **Java inventory is never scaled or reconfigured**, avoiding its
`create-drop` multi-replica hazard. Wasm host mode adds its inventory component
without rerunning data initialization. Both original order-tier counts, the Wasm
version and functional order checks are restored in `finally`, including normal
Ctrl-C. Host mode withdraws the extra HTTP endpoint, deletes only its own
UID-guarded temporary Deployment, restores baseline component allowances and
removes its temporary label/version. Startup event timestamps, pod UID, runtime
ID and image digests are retained in the non-secret state/result files. Do not
edit/deploy these workloads concurrently with the test. If the process is killed,
recover with:

```zsh
python3 monitoring/scaleout/run.py restore monitoring/scaleout/.results/<timestamp>/state.json
```

Tune `--burst-rate`, `--factor`, `--after-scale` or `--p95-ms` to test a different
capacity target; keep the same load/settings for both when comparing them. The
pre-scale interval must contain at least three complete window lengths and the
post-scale interval at least three, ensuring room for full measurement windows.
For the shorter overload comparison:

```zsh
python3 monitoring/scaleout/run.py run both --warmup 30 --after-scale 90 --recovery 15 --burst-rate 400
```

The Wasm scaling semantics are documented in the
[wasmCloud v1 scaling guide](https://wasmcloud.com/docs/v1/tour/deploy-and-scale/).

## Automatic CPU-driven scaling

This test lets CPU measurements trigger the scaling commands, including the
return to one serving unit after load falls. It uses a shared benchmark
controller, **not Kubernetes HPA**: the pinned Wasm operator has no `/scale`
subresource, and the Java pods have no CPU requests configured for a utilization
target. No HPA or cluster-wide CRD changes are installed.

```zsh
python3 monitoring/autoscale/run.py plan
python3 monitoring/autoscale/run.py run --pairs 2
```

To compare a latency-triggered scale-out policy under the same burst:

```zsh
python3 monitoring/autoscale/run.py plan --trigger latency
python3 monitoring/autoscale/run.py run --trigger latency --pairs 2
```

Latency mode scales out after **two consecutive 15-second request-start cohorts
with p95 above 500 ms**, with at least 100 completed requests in each. It waits
20 seconds after each cohort ends to allow the 10-second HTTP timeout and metric
export to finish. Missing, stale, duplicate or nonadjacent cohorts cannot count
as two high-latency windows. This measurement delay is part of the policy and
must be included when comparing trigger times.

Only the scale-out trigger changes. CPU-based scale-in, the healthy-baseline
gate, 250 workers, application resource settings, runtime limits and the
five-minute burst/recovery phases stay the same. CPU alone cannot trigger a
latency-mode scale-out. Keep the two policies' results separate; the comparison
helper rejects attempts to pool different policies.

Open [Java vs Wasm — automatic scaling](http://localhost:8080/grafana/d/java-wasm-autoscale).
For a fresh monitoring installation, `python3 monitoring/footprint/install.py`
provisions this dashboard alongside the other tests.

Each trial warms at 100 orders/s for 180 seconds, holds a 15-second baseline,
bursts to 400/s for 300 seconds, then returns to 100/s for 300 seconds. Both
implementations get 250 order workers, a 10-second request timeout, and a maximum
of two capacity units. Pairs alternate Java-first and Wasm-first. Keep the laptop
on mains with its lid open; the runner inhibits idle sleep on macOS.
The wake assertion does not override clamshell sleep. Closing the lid can pause
Docker and make the cluster/NATS forwarding unavailable. Reopen it before using
the saved-state restoration command below; do not count an interrupted warmup
as an application failure. An incomplete series can be viewed with
`python3 monitoring/autoscale/analyse.py <results-directory> --allow-partial`;
the report retains its incomplete status and restoration warning.

The controller calibrates a per-unit CPU target to 1.25 times the implementation's
median CPU during the last 30 seconds of warmup, with a floor of 50m. It evaluates
only after two completed warmup cohorts pass the baseline throughput/latency
gate; a failed baseline aborts the run instead of setting a target from overload.
The common three-minute warmup avoids calibrating Java from its initial JIT
activity. The controller evaluates
fresh 60-second CPU rates every 15 seconds. The one-minute window accommodates
cAdvisor's observed 12–20-second counter updates despite five-second scrapes.
The factor can be changed with `--cpu-headroom` (default `1.25`). This is a new
protocol: retain earlier results separately rather than pooling the different
triggers or load durations. CPU above 110% of that target (1.375 times the
warmup median, unless the floor applies) requests
scale-out. Once the added capacity serves orders, average CPU below 70% of the
target continuously for 60 seconds requests scale-in. Missing aggregate CPU
signals reset that hold. Each trial permits one automatic 1→2→1 cycle; forced
cleanup is recorded separately and does not count as automatic scale-in.
Load phases set traffic rates only; no phase timer issues a scale command.

Java scales only `order-service`; its CPU signal covers that service. Wasm adds
a complete serving host, with its providers and application components, and
measures CPU in the runtime container. The actual calibrated CPU targets differ:
compare the complete policy and serving outcomes, rather than attributing an
earlier trigger directly to runtime startup. Images may be cached. Databases,
messaging, ingress and platform controllers stay running; existing application
resource settings and the patched runtime timeouts are preserved.

The Wasm actuator reuses the verified scale-out mechanism: the original host
retains its component counts, a temporary host receives its own serving
components, and a direct successful order is required before admitting its HTTP
endpoint. Common pod readiness and direct-order measurements run independently
from CPU decisions and Wasm inventory polling.

The printed `monitoring/autoscale/.results/<timestamp>/report.html` separates
CPU decision time, the new pod's first accepted order, verified serving capacity,
latency recovery, and automatic scale-down completion. Recovery requires two
complete adjacent 15-second request-start cohorts with p95 below 500 ms, zero
failed orders and enough accepted orders to sustain the offered rate. Failed
requests and dropped unsent iterations remain visible, even if scaling itself
succeeds. Raw CPU samples, decisions, readiness probes and cohort results are
saved with the report. An unobserved event is reported as missing, never as zero.

After a completed, restored series, generate CPU/capacity timelines and a compact
comparison with:

```zsh
python3 monitoring/autoscale/analyse.py monitoring/autoscale/.results/<timestamp>
```

Open the resulting `analysis/report.html`. Its event times distinguish the
CPU trigger, new-pod startup and automatic scale-down. Post-capacity healthy
windows do not by themselves establish that scaling caused recovery.

Original capacity, application version and routing are restored after each
trial and after an error. If the process is forcibly killed, recover the active
trial using its private state file:

```zsh
python3 monitoring/autoscale/run.py restore --state monitoring/autoscale/.results/<timestamp>/state.json
```

Policy and recovery checks can run without a cluster:

```zsh
python3 -m unittest discover -s monitoring/autoscale -p 'test_*.py'
```

CPU-only scaling can miss overload when requests wait on RPC, database or other
I/O work. Offered load is not the same as delivered load when k6 runs out of
workers; a missed CPU threshold is not evidence of spare application capacity.
The baseline gate and restoration checks may stop a series. Keep those failed
attempts as evidence rather than combining them with successful startup timings.

An overloaded Wasm host can remain Kubernetes Ready while HTTP calls stall. If
the runner reports a restoration error, inspect its saved logs/state and restore
the original application version/capacity before continuing. Once the temporary
host Deployment is gone and the original host is the sole serving pod, a stalled
original pod may need a process restart:

```zsh
kubectl --context k3d-platform-poc -n wasm get deployments,pods,applications
# Use the exact original host pod name from the preceding output:
kubectl --context k3d-platform-poc -n wasm delete pod <stalled-original-host-pod>
kubectl --context k3d-platform-poc -n wasm rollout status deployment/wasmcloud-host --timeout=180s
k6 run -e IMPLEMENTATION=wasm monitoring/autoscale/smoke.js
```

The smoke test is bounded to 30 seconds at 100 orders/s with ten-second requests
and checks every response. Such a restart is **operational recovery**, not an
automatic scaling success. It preserves the Deployment template and patched
image; do not use it to overwrite failed measurements. The comparison helper
can combine completed trials from multiple runs only when their experiment
hashes and policy settings match; failed sources require explicit verified
recovery provenance and remain linked in the combined report.

## Whole-application scale from zero

This comparison stops all four Java application Deployments, or the single Wasm
serving host, and measures startup through the persistent HTTP routes. Databases,
Redis, NATS, wadm, the operator, ingress and monitoring stay running. Run it while
no other benchmark or deployment operation is active, with the laptop on mains
and its lid open. The runner inhibits idle sleep on macOS.

```zsh
python3 monitoring/scalezero/run.py plan
python3 monitoring/scalezero/run.py run --pairs 5
```

The five pairs alternate which implementation runs first. Each trial verifies
that every selected application pod has disappeared and that orders cannot be
accepted before starting it again. Wasm is stopped through the operator's
`WasmCloudHostConfig.spec.hostReplicas`, so reconciliation does not recreate a
host during the zero-capacity period. Application images, templates, component
counts and the patched runtime's 5-second execution/4-second RPC limits are
preserved. The other implementation remains running at its normal idle baseline.

Open the printed `monitoring/scalezero/.results/<timestamp>/report.html` for
paired bars, medians, ranges and individual trials. It refreshes while running.
The primary metric is seconds from requesting replicas to the first HTTP 201
with the expected order success body. Both versions use a one-second probe
interval and a ten-second request timeout. Native pod readiness and a ten-second
sequence of successful probes are recorded separately. This tests startup at
light probe traffic, rather than latency recovery under a burst.

Container-image caches are retained, with existing pull policies unchanged;
this is fresh-process startup, **not an uncached image-download test**. New Wasm
hosts may still fetch and compile component artifacts. Java's inventory service
uses `create-drop`: its schema creation and demo seeding are included in startup.
The runner refuses to stop Java unless inventory contains exactly the two
original demo rows, and verifies those rows afterwards. Other databases and
caches are not reset. The runtimes therefore perform their existing application
initialization, rather than an identical synthetic startup workload.

The runner restores both applications to one replica and verifies their routes
even after an error. Recovery state is private and ignored by Git; if the
process is forcibly killed, recover with:

```zsh
python3 monitoring/scalezero/run.py restore --state monitoring/scalezero/.results/<timestamp>/state.json
```

The JSON report and per-trial probe/pod records retain failures and invalid
trials. Guard checks can be run without a cluster:

```zsh
python3 -m unittest discover -s monitoring/scalezero -p 'test_*.py'
```

### Cold versus cached container-image startup

To include image download and unpacking in startup, run:

```zsh
python3 monitoring/startup_cache/run.py run --pairs 3
```

Each implementation/pair gets a new tainted k3d worker. The runner checks that
its container-image inventory is empty, measures the cold startup, then stops
and starts the application again on the same worker for the cached measurement.
It verifies that all required images are present before the cached trial and
that the running image IDs are unchanged. Java-first and Wasm-first pairs
alternate; within each pair cold necessarily precedes cached. Both conditions
use fresh application processes, the same placement and existing pull policies.

The runtime image already running in the cluster must also exist in the local
Docker image store. The runner verifies its image ID, publishes that unchanged
build under a run-specific tag in the local registry, and uses its immutable
digest during both Wasm conditions. It also pins the NATS sidecar to its
original digest. It does not rebuild or replace the reliability patch.

Worker provisioning is outside the startup clock. Databases, messaging, ingress,
monitoring and the **local registry** stay warm; these results do not model
Internet downloads of the application images or database cold starts. The pinned
Wasm NATS sidecar still comes from Docker Hub, so its cold pull includes that
network path; the per-trial pull events record it. All four Java application
pods share the temporary worker. Wasm's providers/components are fetched and
compiled by each fresh host in both conditions: the deployed runtime has no
persistent artifact/compilation cache. This isolates the effect of **node
container-image caching**, rather than claiming to test every kind of cache.
Existing cluster workers and their image caches are not pruned.

If a readiness observer fails, retain that raw result and repeat the affected
implementation (for example, `run --pairs 1 --target wasm`). To combine runs
while excluding both members of any incomplete or invalid pair:

```zsh
python3 monitoring/startup_cache/analyse.py monitoring/startup_cache/.results/<first-run> monitoring/startup_cache/.results/<replacement-run>
```

The printed `monitoring/startup_cache/.results/<timestamp>/report.html` contains
live updating bars and separate cold/cached medians. Private recovery state,
image inventories and per-trial probe/pod records are retained alongside it.
The runner restores original templates, image references, replicas and routes,
then removes its temporary workers. If interrupted by a forced kill:

```zsh
python3 monitoring/startup_cache/run.py restore --state monitoring/startup_cache/.results/<timestamp>/state.json
```

The same mains-power, lid-open and inventory-fixture requirements apply as for
the scale-from-zero test. On macOS idle sleep is inhibited throughout the run.

## Restart or remove the local setup

A machine restart or `k3d cluster stop` does not require rebuilding or
redeploying. Start the same Docker runtime/context used to create the cluster,
then start the host dependencies. Inspect the cluster/registry state first:

```zsh
docker info
fish java-containers-k8s/kubernetes/local_k3d/up.fish
k3d cluster list
k3d registry list
```

If stopped, start the existing registry and cluster:

```zsh
docker start k3d-platform-poc.localhost
k3d cluster start platform-poc
kubectl --context k3d-platform-poc get pods -A
curl --fail-with-body --max-time 20 http://localhost:8081/api/product
curl --fail-with-body --max-time 20 http://localhost:8080/products
```

The patched image references, timeouts, ingress and monitoring data remain in
place. Do not reinitialize the Wasm database. If a cluster seems missing, check
`docker context show` before creating a replacement. For a temporary shutdown:

```zsh
k3d cluster stop platform-poc
```

Host PostgreSQL, Redis and NATS remain running. The Java `down.fish` script
stops these shared host services; doing so affects both applications.

For a **complete destructive teardown** of the local comparison cluster and
registry:

```zsh
fish java-containers-k8s/kubernetes/local_k3d/destroy.fish
```

This removes Java, Wasm and monitoring, the local registry artifacts, and
cluster-local PVC data including NATS/Prometheus/Grafana storage. It does not
remove the host PostgreSQL/Redis databases or your ignored signing-key files.
A subsequent full setup must recreate the cluster and republish/import the
artifacts. Existing Wasm database contents still must not be initialized twice.

## Troubleshooting the comparison

| Symptom | Check / next step |
| --- | --- |
| `k3d`, `mvn`, `wash` or another executable is missing | Check `command -v TOOL` and your PATH. The examples use zsh syntax; Fish variable-assignment syntax cannot be pasted into zsh. |
| `DirectoryEntry ... ALIGN32` while installing wash | Install wash 0.38.0 with `cargo +1.84.1 install ... --locked` as above. This is a CLI build problem, before component compilation. |
| Java image publishing cannot resolve the registry | Check `/etc/hosts` for `127.0.0.1 k3d-platform-poc.localhost`, and that the registry responds on port 5001. |
| `unable to initialize nats client` while deploying the application | Check the Wasm platform pods first. The chart config uses `nats-headless.wasm.svc.cluster.local:4222` for wadm; the host address omits the port because operator 0.4.0 appends its client/leaf ports. |
| `ImagePullBackOff` on the patched host | Build the native image and run `fish platform-wasmcloud/k3d/configure-host.fish` to import it into all nodes. A tag existing only in the host Docker image store is insufficient. |
| Provider artifact cannot be pulled | Check the registry and run `fish platform-wasmcloud/k3d/publish-transport.fish`, then redeploy. Do not substitute the stock HTTP provider. |
| 502/504 responses through ingress | Check `platform-poc-http` EndpointSlices against the current Wasm host pod IP. Reapply `http-service.yaml` and `ingress.yaml` or run `fish platform-wasmcloud/k3d/up.fish`; do not route ingress through the operator's legacy selectorless Service. |
| HTTP works but orders fail | Confirm both order POSTs return 201, host database connectivity/credentials, data initialization, and the patched artifact references. Compare constant-response/body-check with orders to isolate downstream work. |
| Java orders fail after a budget rollout; inventory logs say `t_inventory` does not exist | Deploy the local inventory Recreate strategy. A rolling update with `create-drop` can let the departing pod delete the replacement's tables. The budget runner now requires Recreate and checks an actual order before load. |
| Java loses readiness at a budgeted rate | Inspect the per-service CPU caps, not just total CPU. The default order-service cap is 500m, and other services' unused CPU cannot be borrowed. A failed rate is recorded and its logs saved; it is not proof that two pooled CPU cores could not sustain the load. |
| Grafana says `no available server` | Check monitoring pod readiness and previous termination reasons for `OOMKilled`. The committed monitoring setup includes the corrected limits and database settings; rerun `install.py` after updating it. |
| Huge CPU-seconds/1,000 peaks or no CPU-cost line while idle | Use the current generated dashboard. The corrected panel excludes phase transitions and only appears after 30 seconds of continuous load; other resource panels should still show data. |
| Dropped k6 iterations | Orders were never sent because workers were unavailable. Keep the same VU ceiling for both tests; inspect latency, worker demand and generator resources. A zero HTTP-failure count alone does not prove the requested rate was delivered. |
| A long stall across the whole local setup | Check machine sleep/VM pauses. Keep sleep inhibition active; discard interrupted runs rather than interpreting them as application performance. |

Useful read-only checks:

```zsh
kubectl --context k3d-platform-poc -n wasm get pods,applications,services,endpointslices
kubectl --context k3d-platform-poc -n wasm logs deployment/wasmcloud-host \
  --all-containers=true --since=15m
kubectl --context k3d-platform-poc -n wasm logs deployment/wasmcloud-operator --since=15m
kubectl --context k3d-platform-poc -n monitoring get pods
kubectl --context k3d-platform-poc top pods -n java
kubectl --context k3d-platform-poc top pods -n wasm --containers
```
