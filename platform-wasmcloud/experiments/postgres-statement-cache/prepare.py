#!/usr/bin/env python3
"""Prepare a separate checksum-pinned provider tree, leaving old artifacts intact."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parent
BUILD=ROOT/'.build'
SHA='9d225cb039c67766318467818746ba3dda390b7ecb57fd60af7bc34f7bcef4c3'
URL='https://github.com/wasmCloud/wasmCloud/archive/refs/tags/v1.5.1.tar.gz'

def main():
    archive=BUILD/'downloads/wasmCloud-1.5.1';archive.parent.mkdir(parents=True,exist_ok=True)
    if not archive.exists():
        old=ROOT.parent/'transport-registration/.build/downloads/wasmCloud-1.5.1'
        if old.exists():shutil.copyfile(old,archive)
        else:subprocess.run(['curl','--fail','--location','--retry','3','--max-time','120','--output',str(archive),URL],check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA:raise RuntimeError('Upstream checksum mismatch')
    sources=BUILD/'sources'
    if sources.exists():raise RuntimeError('Sources already prepared; refusing to overwrite experimental work')
    scratch=BUILD/'extract';scratch.mkdir()
    with tarfile.open(archive) as tar:tar.extractall(scratch,filter='data')
    sources.mkdir();tree=sources/'provider';(scratch/'wasmCloud-1.5.1').rename(tree);scratch.rmdir()
    target=tree/'crates/provider-sqldb-postgres/src'
    before=hashlib.sha256((target/'lib.rs').read_bytes()).hexdigest()
    manifest_before=hashlib.sha256((target.parent/'Cargo.toml').read_bytes()).hexdigest()
    if 'version = "0.7.2"' not in (target.parent/'Cargo.toml').read_text():raise RuntimeError('Wrong provider version')
    subprocess.run(['patch','--batch','--fuzz=0','-p1','-i',str(ROOT/'patches/provider.patch')],cwd=tree,check=True)
    shutil.copyfile(ROOT/'cached_statement.rs',target/'cached_statement.rs')
    (BUILD/'provenance.json').write_text(json.dumps(dict(upstream=URL,archive_sha256=SHA,
       provider='0.7.2',original_lib_sha256=before,patched_lib_sha256=hashlib.sha256((target/'lib.rs').read_bytes()).hexdigest(),
       original_manifest_sha256=manifest_before,patched_manifest_sha256=hashlib.sha256((target.parent/'Cargo.toml').read_bytes()).hexdigest(),
       cargo_lock_sha256=hashlib.sha256((tree/'Cargo.lock').read_bytes()).hexdigest(),
       patch_sha256=hashlib.sha256((ROOT/'patches/provider.patch').read_bytes()).hexdigest(),
       cache_sha256=hashlib.sha256((ROOT/'cached_statement.rs').read_bytes()).hexdigest(),
       transport='Unmodified upstream dependency; existing host/HTTP transport artifacts are untouched.'),indent=2)+'\n')
    print(tree)

if __name__=='__main__':main()
