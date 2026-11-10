#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
set --local host_image platform-poc/wasmcloud:1.9.2-regack.2
docker image inspect $host_image >/dev/null 2>&1
or begin
    echo 'Build the patched transport first: sh platform-wasmcloud/experiments/transport-registration/build.sh' >&2
    exit 1
end
k3d image import $host_image --cluster platform-poc
or exit $status
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/wasmcloud-host.yaml
or exit $status

# Wait for the operator to reconcile the requested host image before patching it.
kubectl --context k3d-platform-poc --namespace wasm wait deployment/wasmcloud-host \
    '--for=jsonpath={.spec.template.spec.containers[?(@.name=="wasmcloud-host")].image}=platform-poc/wasmcloud:1.9.2-regack.2' \
    --timeout=120s
or exit $status

# The image carries these defaults too; make them visible in the Deployment.
kubectl --context k3d-platform-poc --namespace wasm set env deployment/wasmcloud-host \
    --containers=wasmcloud-host WASMCLOUD_MAX_EXECUTION_TIME_MS=5000 WASMCLOUD_RPC_TIMEOUT_MS=4000
or exit $status
kubectl --context k3d-platform-poc --namespace wasm rollout status deployment/wasmcloud-host --timeout=180s
