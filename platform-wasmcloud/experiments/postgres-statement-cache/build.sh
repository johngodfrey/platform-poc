#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
case $(docker info --format '{{.Architecture}}') in
  aarch64|arm64) PROVIDER_ARCH=aarch64-linux; BUILD_PLATFORM=linux/arm64 ;;
  x86_64|amd64) PROVIDER_ARCH=x86_64-linux; BUILD_PLATFORM=linux/amd64 ;;
  *) echo 'Unsupported Docker architecture' >&2; exit 1 ;;
esac
if [ ! -f .build/provenance.json ]; then python3 prepare.py; fi
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=json.loads(Path('.build/provenance.json').read_text())
for name,key in [('patches/provider.patch','patch_sha256'),('cached_statement.rs','cache_sha256'),('.build/sources/provider/crates/provider-sqldb-postgres/src/lib.rs','patched_lib_sha256'),('.build/sources/provider/crates/provider-sqldb-postgres/src/cached_statement.rs','cache_sha256'),('.build/sources/provider/crates/provider-sqldb-postgres/Cargo.toml','patched_manifest_sha256'),('.build/sources/provider/Cargo.lock','cargo_lock_sha256')]:
    if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=p[key]:raise SystemExit('Prepared source changed: '+name)
PY
mkdir -p .build/artifacts .build/keys
# Reuse only the builder's dependency cache; output and signing identities are separate.
docker build --platform "$BUILD_PLATFORM" --progress=plain \
  --file ../transport-registration/Dockerfile --target artifacts \
  --build-arg SOURCE=provider --build-arg BIN=sqldb-postgres-provider \
  --build-arg FEATURE=provider-sqldb-postgres --build-arg BUILD_JOBS=2 \
  --output type=local,dest=.build/artifacts .build/sources
wash par create --vendor platform-poc --name postgres-statement-cache \
  --version 0.7.2-stmtcache.1 --revision 1 --arch "$PROVIDER_ARCH" \
  --directory .build/keys --binary .build/artifacts/sqldb-postgres-provider \
  --destination .build/artifacts/sqldb-postgres-0.7.2-stmtcache.1.par.gz --compress
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path('.build/artifacts')
(p/'checksums.json').write_text(json.dumps({x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in p.iterdir() if x.is_file() and x.name!='checksums.json'},indent=2)+'\n')
PY
