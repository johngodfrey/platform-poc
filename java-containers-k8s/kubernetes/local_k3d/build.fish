#!/usr/bin/env fish

if not command -sq mvn
    echo 'Maven is not available on PATH. Install it with: brew install maven' >&2
    exit 1
end

set --local SCRIPT_DIR (dirname (realpath (status -f)))

cd $SCRIPT_DIR/../..
or exit $status

mvn clean package
