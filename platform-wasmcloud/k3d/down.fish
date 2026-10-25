#!/usr/bin/env fish

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
        if test -n "$PID"; and string match --quiet '*kubectl*--namespace wasm port-forward*' (ps -p $PID -o command=)
            echo "Killing $name with PID $PID"
            kill $PID
        end
    end
    popd
end

# Stop optional NATS forwarding and any legacy HTTP forwarding.
# Do not stop shared PostgreSQL, Redis, or Java's NATS server.
section "stopping Wasm HTTP forwarding"
stop platform-poc-wasm-http

section "stopping Wasm NATS forwarding"
stop platform-poc-wasm-nats

echo 'Persistent HTTP ingress remains active. Use undeploy.fish to remove the application and its ingress.'
