# Dockerfile per eseguire multiple_runs.py con il backend Rust nshift.
#
# Build:
#   docker build -t carbonshift-battery -f tests/battery/Dockerfile .
#
# Run (monta la cartella results per persistere gli output):
#   docker run --rm -v "$(pwd)/tests/battery/results:/app/tests/battery/results" carbonshift-battery

# ─── stage 1: build del binario Rust nshift ───────────────────────────────────
FROM rust:1.86-slim-bookworm AS rust-builder

WORKDIR /build

# Copia solo i file necessari per compilare il crate Rust.
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/src ./src

# Compila il binario in release.
RUN cargo build --release --bin nshift

# ─── stage 2: immagine Python con il runtime per run_battery.py ───────────────
FROM python:3.12-slim-bookworm

WORKDIR /app

# Installa dipendenze di sistema minime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# NOTA: multiple_runs.py, run_battery.py e scenario_io.py usano solo la
# libreria standard di Python, quindi non è necessario installare
# requirements.txt (che contiene dipendenze pesanti/incompatibili con Python 3.12).

# Copia solo i file necessari (filtrati da .dockerignore nella repo root).
COPY . .

# Copia il binario Rust compilato dallo stage precedente nella posizione attesa
# da battery_config.json (rust/target/release/nshift).
COPY --from=rust-builder /build/target/release/nshift /app/rust/target/release/nshift

# Imposta la working directory sulla root del repo, dove run_battery.py e
# multiple_runs.py si aspettano di essere eseguiti.
WORKDIR /app

# Comando di default: esegue la griglia di run definita in multiple_runs.py.
CMD ["python", "tests/battery/multiple_runs.py"]
