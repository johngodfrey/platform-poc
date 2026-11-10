#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))

# Stop the old pods before starting java namespace pods: inventory uses create-drop.
# Leave default namespace, unrelated resources, and old secrets intact.
kubectl --context k3d-platform-poc --namespace default delete deployment inventory-service notification-service order-service product-service --ignore-not-found --wait=true
or exit $status
for service in inventory order product notification
    set --local pods (kubectl --context k3d-platform-poc --namespace default get pods -l service=$service -o name)
    or exit $status
    if test (count $pods) -gt 0
        kubectl --context k3d-platform-poc --namespace default wait --for=delete $pods --timeout=120s
        or exit $status
    end
end
kubectl --context k3d-platform-poc --namespace default delete service,ingress inventory-service order-service product-service --ignore-not-found
or exit $status
fish $SCRIPT_DIR/deploy.fish
