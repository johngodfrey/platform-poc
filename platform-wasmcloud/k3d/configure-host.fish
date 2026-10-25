#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/wasmcloud-host.yaml
or exit $status

# Wait for the operator to reconcile the requested host image before patching it.
kubectl --context k3d-platform-poc --namespace wasm wait deployment/wasmcloud-host \
    '--for=jsonpath={.spec.template.spec.containers[?(@.name=="wasmcloud-host")].image}=ghcr.io/wasmcloud/wasmcloud:1.9.2' \
    --timeout=120s
or exit $status

# Operator 0.3.x has no host environment override in WasmCloudHostConfig.
# Reapply this setting whenever the operator regenerates the Deployment.
kubectl --context k3d-platform-poc --namespace wasm set env deployment/wasmcloud-host \
    --containers=wasmcloud-host WASMCLOUD_MAX_EXECUTION_TIME_MS=5000
or exit $status
kubectl --context k3d-platform-poc --namespace wasm rollout status deployment/wasmcloud-host --timeout=180s
