#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))

function section
    echo
    string pad --right --char=— -w$COLUMNS "———— $argv ————"
end

function stop
    set -l name $argv[1]
    pushd /tmp
    if test -f {$name}.pid
        set -l PID (cat {$name}.pid)
        rm -f {$name}.pid {$name}.out
        if test -n "$PID"
            echo "Killing $name with PID $PID"
            kill $PID
        end
    end
    popd
end

section "undeploying platform-poc"
# Preserve the namespace; remove only this application's resources.
kubectl --context k3d-platform-poc --namespace java delete deployment,service,ingress inventory-service order-service product-service --ignore-not-found
or exit $status
kubectl --context k3d-platform-poc --namespace java delete deployment notification-service --ignore-not-found
or exit $status
kubectl --context k3d-platform-poc --namespace java delete secret -l app.kubernetes.io/part-of=platform-poc-java
