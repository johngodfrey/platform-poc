# syntax=docker/dockerfile:1
ARG RUST_VERSION=1.87.0
FROM rust:${RUST_VERSION}-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config libssl-dev cmake clang ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG SOURCE
ARG BIN
ARG FEATURE
ARG RUST_VERSION
ARG BUILD_JOBS=2
ENV RUSTUP_TOOLCHAIN=${RUST_VERSION} CARGO_BUILD_JOBS=${BUILD_JOBS} CARGO_TARGET_DIR=/target \
    CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16
WORKDIR /src
COPY ${SOURCE}/ ./
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/target,id=registration-${SOURCE} \
    cargo build --locked --release --no-default-features --features "$FEATURE" --bin "$BIN" \
    && mkdir /out && cp "/target/release/$BIN" /out/

FROM scratch AS artifacts
COPY --from=builder /out/ /

FROM debian:bookworm-slim AS host
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/wasmcloud /usr/local/bin/wasmcloud
LABEL org.opencontainers.image.title="wasmCloud registration acknowledgement experiment" \
      org.opencontainers.image.version="1.9.2-regack.2"
# Keep execution bounds across operator-created Deployment replacements.
ENV WASMCLOUD_MAX_EXECUTION_TIME_MS=5000 WASMCLOUD_RPC_TIMEOUT_MS=4000
ENTRYPOINT ["/usr/local/bin/wasmcloud"]
