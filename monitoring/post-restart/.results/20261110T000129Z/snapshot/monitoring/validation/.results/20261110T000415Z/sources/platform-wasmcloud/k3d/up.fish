#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
argparse 'n/nats' -- $argv
or exit 2

function section
    echo
    string pad --right --char=— -w$COLUMNS "———— $argv ————"
end

function daemon
    pushd /tmp
    status job-control full
    set -l name $argv[1]
    if test -f {$name}.pid
        set -l PID (cat {$name}.pid)
        rm -f {$name}.pid {$name}.out
        if test -n "$PID"; and string match --quiet '*kubectl*--namespace wasm port-forward*' (ps -p $PID -o command=)
            echo "Killing $name with PID $PID"
            kill $PID
        end
    end
    set -l command $argv[2..-1]
    command nohup $command >{$name}.out 2>&1 &
    echo {$name}...
    echo $last_pid >{$name}.pid
    set -l forwarding_ready false
    for attempt in (seq 50)
        if string match --quiet '*Forwarding from*' <{$name}.out
            set forwarding_ready true
            break
        end
        if not kill -0 $last_pid 2>/dev/null
            break
        end
        sleep 0.1
    end
    cat {$name}.out
    if test "$forwarding_ready" != true
        echo "Port forwarding failed to start for $name." >&2
        kill $last_pid 2>/dev/null
        rm -f {$name}.pid
        popd
        return 1
    end
    popd
end

# PostgreSQL and Redis are shared host services; leave their lifecycle to the user.
fish $SCRIPT_DIR/expose-http.fish
or exit $status
section "checking Wasm HTTP service"
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/http-service.yaml
or exit $status

section "configuring persistent Wasm HTTP ingress"
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/ingress.yaml
or exit $status
curl --fail-with-body --silent --show-error --connect-timeout 5 --max-time 20 http://localhost:8080/products >/dev/null
or exit $status
echo 'Wasm HTTP is available at http://localhost:8080 (no port forward required).'

# Optional developer access to the control plane; HTTP does not depend on it.
if set --query _flag_nats
    section "forwarding Wasm NATS on localhost:14222 and 14223"
    daemon platform-poc-wasm-nats kubectl --context k3d-platform-poc --namespace wasm port-forward service/nats 14222:4222 14223:4223
    or exit $status
end
