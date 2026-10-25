#!/usr/bin/env python3
"""Prepare checksum-pinned sources with only local transport overrides."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".build"
INPUTS = {
    "wasmCloud-1.9.2": (
        "https://github.com/wasmCloud/wasmCloud/archive/refs/tags/v1.9.2.tar.gz",
        "aac79480ad4b7d6cdce0c2b42ccb34a8237a1f63a34cf9fa1642eb3153947c44",
    ),
    "wasmCloud-1.5.1": (
        "https://github.com/wasmCloud/wasmCloud/archive/refs/tags/v1.5.1.tar.gz",
        "9d225cb039c67766318467818746ba3dda390b7ecb57fd60af7bc34f7bcef4c3",
    ),
    "wrpc-transport-nats-0.29.0": (
        "https://static.crates.io/crates/wrpc-transport-nats/wrpc-transport-nats-0.29.0.crate",
        "19310c1e0d7f2b8cc25bc700d3c4ab6b58c97cfe70fa490013a6a2d18de8a0a8",
    ),
    "wrpc-transport-nats-0.27.1": (
        "https://static.crates.io/crates/wrpc-transport-nats/wrpc-transport-nats-0.27.1.crate",
        "a226edc7f6fd721b10260aa7fe083c71ccb29d464eb8f7121d634afc5e8c355b",
    ),
}


def extract(name, destination):
    url, checksum = INPUTS[name]
    archive = BUILD / "downloads" / name
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        print(f"Downloading {name}", flush=True)
        with urllib.request.urlopen(url, timeout=120) as source, archive.open("wb") as out:
            shutil.copyfileobj(source, out)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
        raise RuntimeError(f"Checksum mismatch: {archive}")
    destination.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        # Extraction rejects traversal paths, devices and unsafe links.
        tar.extractall(destination, filter="data")
    return destination / name


def main():
    sources = BUILD / "sources"
    if sources.exists():
        shutil.rmtree(sources)
    sources.mkdir(parents=True)
    for kind, release, transport in [("host", "1.9.2", "0.29.0"),
                                     ("provider", "1.5.1", "0.27.1")]:
        scratch = sources / f"extract-{kind}"
        tree = extract(f"wasmCloud-{release}", scratch)
        tree.rename(sources / kind)
        scratch.rmdir()
        tree = sources / kind
        vendor = extract(f"wrpc-transport-nats-{transport}", tree / "registration-patch")
        patch = ROOT / "patches" / f"wrpc-transport-nats-{transport}.patch"
        subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=vendor, check=True)
        # This stress test is compiled only by cargo test, never into either binary.
        with (vendor / "src/lib.rs").open("a") as out:
            out.write('\n#[cfg(test)]\n#[path = "registration_stress.rs"]\nmod registration_stress;\n')
        shutil.copyfile(ROOT / "registration_stress.rs", vendor / "src/registration_stress.rs")
        with (vendor / "Cargo.toml").open("a") as out:
            out.write('\n[dev-dependencies.tokio]\nversion = "1"\nfeatures = ["time"]\n')
        with (tree / "Cargo.toml").open("a") as out:
            out.write(f'\n[patch.crates-io]\nwrpc-transport-nats = {{ path = "registration-patch/wrpc-transport-nats-{transport}" }}\n')
        # A path override removes just this package's registry source/checksum.
        lock = tree / "Cargo.lock"
        blocks = lock.read_text().split("[[package]]")
        matches = 0
        for i, block in enumerate(blocks):
            if block.startswith(f'\nname = "wrpc-transport-nats"\nversion = "{transport}"\n'):
                blocks[i] = "\n".join(line for line in block.split("\n")
                                      if not line.startswith(("source =", "checksum =")))
                matches += 1
        if matches != 1:
            raise RuntimeError(f"Expected exactly one transport package in {lock}")
        lock.write_text("[[package]]".join(blocks))
        # Test as a standalone package so its default features cannot expand the
        # production workspace's dependency resolution.
        tests = BUILD / "tests" / kind
        if tests.exists():
            shutil.rmtree(tests)
        shutil.copytree(vendor, tests)
        with (tests / "Cargo.toml").open("a") as out:
            out.write("\n[workspace]\n")
        shutil.copyfile(lock, tests / "Cargo.lock")
        print(f"Prepared {kind}: release {release}, transport {transport}", flush=True)
    (BUILD / "provenance.json").write_text(json.dumps(INPUTS, indent=2) + "\n")


if __name__ == "__main__":
    main()
