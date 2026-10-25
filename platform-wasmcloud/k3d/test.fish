#!/usr/bin/env fish

set --local endpoint http://localhost:8080
for path in products 'inventory?skus=ENG-V8-500' orders
    curl --fail-with-body --silent --show-error --connect-timeout 5 --max-time 20 "$endpoint/$path"
    or exit $status
    echo
end
