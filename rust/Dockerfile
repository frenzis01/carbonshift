# syntax=docker/dockerfile:1

# ---- builder -----------------------------------------------------------
FROM rust:1-bookworm AS builder
WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY src ./src

RUN cargo build --release --locked --bin carbonshift-service

# ---- runtime -------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 carbonshift

COPY --from=builder /app/target/release/carbonshift-service /usr/local/bin/carbonshift-service

USER carbonshift
ENV LISTEN_ADDR=0.0.0.0:8080
EXPOSE 8080

# Liveness only (process up); readiness (horizon exhaustion) is /ready.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/local/bin/carbonshift-service"]
