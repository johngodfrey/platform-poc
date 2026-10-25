#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
set --local COMPONENT_DIR (realpath $SCRIPT_DIR/../wasm-components)

for dependency in wash cargo rustup curl
    if not command -sq $dependency
        printf 'Required command not found: %s. See k3d/README.md.\n' $dependency >&2
        exit 1
    end
end

set --local wash_version (wash --version | string match --regex '0\.[0-9]+\.[0-9]+' | head -n 1)
if test -z "$wash_version"
    echo 'This application requires the wasmCloud 1.x wash CLI (use wash 0.38.0).' >&2
    exit 1
end
set --local wash_minor (string split '.' $wash_version)[2]
if test $wash_minor -lt 38
    echo 'wash 0.38.0 or newer within the 0.x series is required for registry.pull.sources.' >&2
    exit 1
end
if not rustup target list --installed | string match --quiet wasm32-wasip1
    echo 'Missing Rust target. Run: rustup target add wasm32-wasip1' >&2
    exit 1
end

# The existing k3d registry is exposed on the host at port 5001.
# Keep repository names aligned with k3d/wadm.yaml and the local wadm.yaml.
curl --fail --silent --show-error http://localhost:5001/v2/ >/dev/null
or exit $status

for component in data-init inventory-service orders-service products-service http-controller notification-service
    pushd $COMPONENT_DIR/$component
    or exit $status
    wash build
    or exit $status
    set --local artifact (string replace -a '-' '_' $component)
    if not test -f build/{$artifact}_s.wasm
        printf 'Signed component missing: %s/build/%s_s.wasm\n' $component $artifact >&2
        exit 1
    end
    set --local artifact_version 0.1.0
    if test $component = http-controller
        set artifact_version 0.1.3
    end
    wash push --insecure localhost:5001/v2/platform-poc/{$artifact}:{$artifact_version} build/{$artifact}_s.wasm
    or exit $status
    popd
    or exit $status
end
