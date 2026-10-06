#!/usr/bin/env fish

function section
    echo
    string pad --right --char=— -w$COLUMNS "———— $argv ————"
end

for dependency in k3d kubectl docker
    if not command -sq $dependency
        printf 'Required command not found on PATH: %s\n' $dependency >&2
        echo 'See README.md for setup instructions.' >&2
        exit 1
    end
end

if not docker info >/dev/null 2>&1
    echo 'Docker is not available. Start your Docker runtime and retry.' >&2
    exit 1
end

section "starting registry"
k3d registry create platform-poc.localhost --port 5001
or exit $status

section "starting platform-poc cluster"
k3d cluster create platform-poc \
    --agents 2 \
    --registry-use k3d-platform-poc.localhost:5001 \
    --api-port 6550 \
    -p "8081:80@loadbalancer"
or exit $status

section configuration
kubectl --context k3d-platform-poc cluster-info
