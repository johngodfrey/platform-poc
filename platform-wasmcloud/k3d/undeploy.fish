#!/usr/bin/env fish

# Delete by name so cleanup does not depend on the current credentials file.
kubectl --context k3d-platform-poc --namespace wasm delete ingress platform-poc --ignore-not-found
or exit $status
kubectl --context k3d-platform-poc --namespace wasm delete service platform-poc-http --ignore-not-found
or exit $status
kubectl --context k3d-platform-poc --namespace wasm delete applications.core.oam.dev platform-poc --ignore-not-found
