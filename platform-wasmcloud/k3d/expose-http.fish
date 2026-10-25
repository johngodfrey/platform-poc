#!/usr/bin/env fish

# Docker port mappings survive shell exit and follow the cluster load balancer.
for dependency in docker k3d
    if not command -sq $dependency
        printf 'Required command not found: %s\n' $dependency >&2
        exit 1
    end
end

set --local mappings (docker port k3d-platform-poc-serverlb 80/tcp)
or exit $status
if string match --quiet '*:8080' $mappings
    exit 0
end

echo 'Adding persistent localhost:8080 access; the cluster load balancer will briefly restart.'
k3d cluster edit platform-poc --port-add '8080:80@loadbalancer'
