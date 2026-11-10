#!/usr/bin/env python3
"""Run real NATS transport stress tests without connecting to the k3d cluster."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", action="store_true", help="also test unpatched transports")
    args = parser.parse_args()
    name = "platform-poc-regack-test-" + uuid.uuid4().hex[:10]
    subprocess.run(["docker", "run", "--detach", "--name", name,
                    "--publish", "127.0.0.1::4222", "nats:2.10.19-alpine"], check=True)
    try:
        address = subprocess.check_output(["docker", "port", name, "4222/tcp"], text=True).strip()
        env = dict(os.environ, REGISTRATION_TEST_NATS_URL="nats://" + address,
                   CARGO_TARGET_DIR=str(ROOT / ".build/test-target"), CARGO_BUILD_JOBS="2")
        for kind, version, feature in [("host", "0.29.0", "async-nats-0_39"),
                                       ("provider", "0.27.1", "async-nats-0_36")]:
            for control in ([False, True] if args.control else [False]):
                source = ROOT / ".build/tests" / kind
                if not source.exists():
                    raise RuntimeError("Run prepare.py before test.py")
                label = kind + ("-control" if control else "-patched")
                if control:
                    destination = source.with_name(kind + "-control")
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("target"))
                    source = destination
                    subprocess.run(["patch", "--batch", "--reverse", "-p1", "-i",
                                    str(ROOT / "patches" / f"wrpc-transport-nats-{version}.patch")],
                                   cwd=source, check=True)
                log = ROOT / ".build" / (label + "-stress.log")
                with log.open("w") as out:
                    result = subprocess.run(["cargo", "test", "--manifest-path", str(source / "Cargo.toml"),
                                             "--no-default-features", "--features", feature,
                                             "--lib", "concurrent_handshakes_preserve_fast_replies",
                                             "--", "--nocapture"], env=env, stdout=out,
                                            stderr=subprocess.STDOUT, timeout=600)
                print(f"{label}: exit={result.returncode}; {log}", flush=True)
                if result.returncode and not control:
                    raise RuntimeError(f"Patched transport failed: {log}")
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True)


if __name__ == "__main__":
    main()
