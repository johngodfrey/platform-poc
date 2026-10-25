#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
python3 $SCRIPT_DIR/render-application.py
or exit $status
fish $SCRIPT_DIR/publish-transport.fish
or exit $status
fish $SCRIPT_DIR/expose-http.fish
or exit $status
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/.generated/wadm.yaml
or exit $status
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/http-service.yaml
or exit $status
kubectl --context k3d-platform-poc --namespace wasm apply -f $SCRIPT_DIR/ingress.yaml
