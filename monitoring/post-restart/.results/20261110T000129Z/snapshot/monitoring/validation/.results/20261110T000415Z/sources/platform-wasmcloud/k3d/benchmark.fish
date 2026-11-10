#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
set --export PLATFORM_POC_ENDPOINT http://localhost:8080
fish $SCRIPT_DIR/../benchmark.fish
