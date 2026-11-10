#!/usr/bin/env fish

set --local endpoint http://localhost:8080
if set --query PLATFORM_POC_ENDPOINT
    set endpoint $PLATFORM_POC_ENDPOINT
end

oha "$endpoint/orders" \
    -c 100 \
    -n 10000 \
    -m POST \
    -H 'Content-Type: application/json' \
    -d '
    [
      {
        "sku": "ENG-V8-500",
        "price": 8500,
        "quantity": 1
      },
      {
        "sku": "BRK-PD-HP",
        "price": 150,
        "quantity": 3
      }
    ]'
