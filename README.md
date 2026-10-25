# Platform Proof of Concept

_Comparison of different deployment architectures for a small microservices
application._

1. Java Spring Boot, Containers, Kubernetes
   [example](./java-containers-k8s/README.md)
1. Rust, Containers, Kubernetes [example](./rust-containers-k8s/README.md)
1. Rust, WebAssembly (Wasm) Components [example](./wasm-components/README.md)
   - in wasmCloud on VMs [example](./platform-wasmcloud/README.md)
   - in wasmCloud on Kubernetes, with the wasmCloud Operator
     [example](./platform-wasmcloud/README.md)
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

The Kubernetes comparison uses the registration-ack transport patches in
`platform-wasmcloud/experiments/transport-registration`, persistent HTTP routing,
250 order workers, and a five-second Wasm execution limit. The local setup has
been exercised on macOS with an ARM64 Docker engine. The native build also
accepts AMD64, but that path has not been integration-tested here. Linux users
must additionally arrange for `host.docker.internal` inside the k3d nodes to
resolve to their host and make the host databases/message broker reachable.

Install a Docker runtime, Fish, k3d, kubectl, Helm, Python 3.11.8+, curl, patch,
Maven, JDK 17, Rust/rustup, k6, and wash 0.38.0. The tested k3d version is 5.9.0;
cluster creation pins the tested K3s image `rancher/k3s:v1.36.4-k3s1`. See the
[Wasm prerequisites](platform-wasmcloud/k3d/README.md#build-prerequisites) for the
pinned wash installation command. Rust compilers for the native host/provider
are installed inside their Docker builds. Set `JAVA_HOME` to JDK 17 and add
`127.0.0.1 k3d-platform-poc.localhost` to `/etc/hosts` for Java's image publishing.

From the repository root, create the shared cluster once:

```zsh
fish java-containers-k8s/kubernetes/local_k3d/create.fish
fish java-containers-k8s/kubernetes/local_k3d/up.fish
rustup target add wasm32-wasip1
```

The `up.fish` command uses Homebrew to start PostgreSQL 15, Redis and NATS on
macOS. Create the `inventory-service`, `order-service` and `platform-poc-wasm`
PostgreSQL roles/databases with passwords of your choice. For example, use
`createuser --pwprompt ROLE` followed by `createdb --owner=ROLE DATABASE` using
your PostgreSQL administrator account. Configure the host services to accept
connections from the container runtime. Then copy and edit the two credential
templates, using the credentials for those databases:

```zsh
cp java-containers-k8s/kubernetes/manifests/overlays/local/.env.secret.example \
   java-containers-k8s/kubernetes/manifests/overlays/local/.env.secret
cp platform-wasmcloud/k3d/.env.secret.example platform-wasmcloud/k3d/.env.secret
```

Build and deploy both implementations:

```zsh
fish java-containers-k8s/kubernetes/local_k3d/build.fish
fish java-containers-k8s/kubernetes/local_k3d/deploy.fish
sh platform-wasmcloud/experiments/transport-registration/build.sh
fish platform-wasmcloud/build_and_push.fish
fish platform-wasmcloud/k3d/install-operator.fish
fish platform-wasmcloud/k3d/deploy.fish
```

The transport build verifies source checksums and builds both patched binaries
locally. Expect a substantial first-build time and disk use; keep `.build/keys`
and component signing keys for subsequent builds. These files and generated
credentials are ignored by Git. The installer imports the host image into all
k3d nodes; deployment pushes the patched provider to the local registry.

Wait for the application to report `Deployed` and `/products` to return 200:

```zsh
kubectl --context k3d-platform-poc -n wasm get applications
curl --fail-with-body --max-time 20 http://localhost:8080/products
# ONCE for a new Wasm database; repeating this inserts more inventory rows.
curl --fail-with-body --max-time 60 http://localhost:8080/data-init/all
fish java-containers-k8s/kubernetes/local_k3d/test.fish
fish platform-wasmcloud/k3d/test.fish
```

Run the five-minute, 100-orders/second benchmarks sequentially. Both default
to 250 VUs. On macOS, prevent sleep throughout each test:

```zsh
caffeinate -i -s k6 run orders_java.js
caffeinate -i -s k6 run orders_wasm.js
```

On other operating systems, use the corresponding sleep inhibitor or run k6
directly on a machine that will remain awake. Shared Postgres/Redis and the
host NATS server must stay running. For the three-minute footprint demo and
live dashboard, continue below.

## Live Java/Wasm footprint demonstration

The local `k3d-platform-poc` setup includes a provisioned Grafana dashboard and
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

The Prometheus configuration, recording rules, datasource and dashboard are
generated by `monitoring/footprint/generate.py`; the generated dashboard and
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
