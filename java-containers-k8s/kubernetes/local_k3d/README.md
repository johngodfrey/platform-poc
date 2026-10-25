# Platform PoC Java containers in a local k3d kubernetes cluster

## Prerequisites

Install Fish, k3d, kubectl, Maven, and JDK 17, and start a Docker-compatible runtime.
On macOS with Homebrew:

```fish
brew install fish k3d kubectl maven openjdk@17
set -gx JAVA_HOME (brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home
mvn -version
docker info
```

Cluster creation stops if a prerequisite or creation command fails. A connection
refused error from `kubectl` after failed creation refers to an existing kubeconfig
endpoint; it does not mean a new cluster was created.

## Namespace and migration

The local overlay and scripts deploy into `java` on `k3d-platform-poc`. The
namespace is created by the overlay. Java's registry and HTTP port 8081 are
shared with the comparison setup. Wasm uses a separate `wasm` namespace and
HTTP port 8080 for `/products`, `/inventory`, `/orders`, and `/data-init`. Both
ports map persistently to Traefik; Java keeps its `/api/...` routes on port 8081.
See [the Wasm instructions](../../../platform-wasmcloud/k3d/README.md).

If the application is already in `default`, run this once from this directory:

```zsh
./migrate-namespace.fish
```

It stops the old pods, removes their services/ingresses, and deploys into `java`.
This causes downtime and inventory's current `create-drop` configuration
recreates its demo tables. It preserves unrelated resources and old secrets in
`default`; remove unused `platform-poc-secrets-…` there by their exact names.
Do not leave both deployments running against the same inventory database.

Inspect the deployment and measurements with:

```zsh
kubectl --context k3d-platform-poc -n java get pods,services,ingresses
kubectl --context k3d-platform-poc -n java top pods
```

`undeploy.fish` removes the application resources and its labelled secrets,
while preserving the namespace. `destroy.fish` removes the shared cluster and
registry, including any Wasm deployment.

## Run

1. Create the cluster with a registry

   ```fish
   ./create.fish
   ```

1. Add the registry to your local /etc/hosts

   ```fish
   # edit /etc/hosts and add the following line
   # 127.0.0.1 k3d-platform-poc.localhost
   sudo vim /etc/hosts
   ```

1. Start redis, postgres, NATS

   ```fish
   ./up.fish
   ```

1. Build the platform-poc application, and push to local registry

   ```fish
    ./build.fish
   ```

1. Install the platform-poc application

   ```fish
   ./deploy.fish
   ```

1. Delete the application

   ```fish
   ./undeploy.fish
   ```

1. Stop the redis, postgres, NATS

   ```fish
    ./down.fish
   ```

1. Delete the cluster

   ```fish
     ./destroy.fish
   ```

## Test

1. Run the test

   ```fish
   ./test.fish
   ```
