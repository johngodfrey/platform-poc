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
