#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
if [ ! -f .build/provenance.json ]; then python3 prepare.py; fi
python3 - <<'PY'
from pathlib import Path
import hashlib,json,shutil,tarfile
r=Path.cwd();source=r/'.build/sources/provider';target=r/'.build/control-sources/provider';p=json.loads((r/'.build/provenance.json').read_text())
if not target.exists():
    shutil.copytree(source,target)
    name='crates/provider-sqldb-postgres/src/lib.rs'
    with tarfile.open(r/'.build/downloads/wasmCloud-1.5.1') as t:(target/name).write_bytes(t.extractfile('wasmCloud-1.5.1/'+name).read())
    (target/'crates/provider-sqldb-postgres/src/cached_statement.rs').unlink()
for name,key in [('crates/provider-sqldb-postgres/src/lib.rs','original_lib_sha256'),('crates/provider-sqldb-postgres/Cargo.toml','patched_manifest_sha256'),('Cargo.lock','cargo_lock_sha256')]:
    if hashlib.sha256((target/name).read_bytes()).hexdigest()!=p[key]:raise SystemExit('Control source mismatch: '+name)
(r/'.build/control-provenance.json').write_text(json.dumps(dict(description='Same-build uncached control',source_lib_sha256=p['original_lib_sha256'],manifest_sha256=p['patched_manifest_sha256'],cargo_lock_sha256=p['cargo_lock_sha256']),indent=2)+'\n')
PY
case $(docker info --format '{{.Architecture}}') in
  aarch64|arm64) PROVIDER_ARCH=aarch64-linux; BUILD_PLATFORM=linux/arm64 ;;
  x86_64|amd64) PROVIDER_ARCH=x86_64-linux; BUILD_PLATFORM=linux/amd64 ;;
  *) echo 'Unsupported Docker architecture' >&2; exit 1 ;;
esac
mkdir -p .build/artifacts/control .build/control-keys
docker build --platform "$BUILD_PLATFORM" --progress=plain \
  --file ../transport-registration/Dockerfile --target artifacts \
  --build-arg SOURCE=provider --build-arg BIN=sqldb-postgres-provider \
  --build-arg FEATURE=provider-sqldb-postgres --build-arg BUILD_JOBS=2 \
  --output type=local,dest=.build/artifacts/control .build/control-sources
wash par create --vendor platform-poc --name postgres-statement-cache-control \
  --version 0.7.2-stmtcache-control.1 --revision 1 --arch "$PROVIDER_ARCH" \
  --directory .build/control-keys --binary .build/artifacts/control/sqldb-postgres-provider \
  --destination .build/artifacts/control/sqldb-postgres-0.7.2-stmtcache-control.1.par.gz --compress
