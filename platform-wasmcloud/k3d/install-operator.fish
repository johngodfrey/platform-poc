#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
for dependency in helm kubectl docker k3d
    if not command -sq $dependency
        printf 'Required command not found: %s\n' $dependency >&2
        exit 1
    end
end
if not docker image inspect platform-poc/wasmcloud:1.9.2-regack.2 >/dev/null 2>&1
    echo 'Build the patched transport before installing the platform. See k3d/README.md.' >&2
    exit 1
end

# Pin the chart that supports this repo's wasmCloud 1.x/OAM manifests.
helm upgrade --install wasmcloud-platform \
    oci://ghcr.io/wasmcloud/charts/wasmcloud-platform \
    --version 0.1.2 \
    --kube-context k3d-platform-poc \
    --namespace wasm --create-namespace \
    --values $SCRIPT_DIR/platform-values.yaml \
    --wait --timeout 10m
or exit $status

# Keep the host in a standalone manifest; do not also enable Helm hostConfig.
fish $SCRIPT_DIR/configure-host.fish
or exit $status
kubectl --context k3d-platform-poc --namespace wasm get wasmcloudhostconfig wasmcloud-host
