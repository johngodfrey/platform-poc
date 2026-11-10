#!/usr/bin/env fish

set --local SCRIPT_DIR (dirname (realpath (status -f)))
set --local archive $SCRIPT_DIR/../experiments/transport-registration/.build/artifacts/http-server-0.25.0-regack.1.par.gz
if not test -f $archive
    echo 'Build the patched transport first: sh platform-wasmcloud/experiments/transport-registration/build.sh' >&2
    exit 1
end
curl --fail --silent --show-error http://localhost:5001/v2/ >/dev/null
or exit $status
wash push --insecure localhost:5001/v2/platform-poc/http-server:0.25.0-regack.1 $archive
or exit $status
