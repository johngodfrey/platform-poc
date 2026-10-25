#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))

function section
    echo
    string pad --right --char=— -w$COLUMNS "———— $argv ————"
end

section "deploying platform-poc"
kubectl --context k3d-platform-poc --namespace java apply -k $SCRIPT_DIR/../manifests/overlays/local
