#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
case $(docker info --format '{{.Architecture}}') in
  aarch64|arm64) ;;
  *) echo 'This experiment currently builds Linux ARM64 artifacts only.' >&2; exit 1 ;;
esac
python3 prepare.py
mkdir -p .build/artifacts .build/keys
docker build --progress=plain --file Dockerfile --target artifacts \
  --build-arg SOURCE=provider --build-arg BIN=http-server-provider \
  --build-arg FEATURE=provider-http-server \
  --output type=local,dest=.build/artifacts/provider .build/sources
docker build --progress=plain --file Dockerfile --target host \
  --build-arg RUST_VERSION=1.95.0 --build-arg BUILD_JOBS=1 \
  --build-arg SOURCE=host --build-arg BIN=wasmcloud --build-arg FEATURE=wasmcloud \
  --tag platform-poc/wasmcloud:1.9.2-regack.1 .build/sources
# Separate experiment keys avoid changing the identity of the stock provider.
wash par create --vendor platform-poc --name http-server-registration-ack \
  --version 0.25.0-regack.1 --revision 1 --arch aarch64-linux \
  --directory .build/keys --binary .build/artifacts/provider/http-server-provider \
  --destination .build/artifacts/http-server-0.25.0-regack.1.par.gz --compress
python3 - <<'PY'
import hashlib, json, subprocess
from pathlib import Path
def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
image = 'platform-poc/wasmcloud:1.9.2-regack.1'
record = {
    'host': {'source': 'wasmCloud v1.9.2', 'transport': '0.29.0',
             'image': image, 'image_id': subprocess.check_output(
                 ['docker', 'image', 'inspect', '--format', '{{.Id}}', image], text=True).strip()},
    'provider': {'source': 'wasmCloud v1.5.1', 'version': '0.25.0-regack.1',
                 'transport': '0.27.1', 'architecture': 'aarch64-linux',
                 'binary_sha256': checksum('.build/artifacts/provider/http-server-provider'),
                 'archive_sha256': checksum('.build/artifacts/http-server-0.25.0-regack.1.par.gz')},
    'patch_sha256': {str(p): checksum(p) for p in sorted(Path('patches').glob('*.patch'))},
}
Path('.build/artifacts/build.json').write_text(json.dumps(record, indent=2) + '\n')
PY
