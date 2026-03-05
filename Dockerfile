# Multi-stage Dockerfile for Sovereign-OS (Python 3.12)
# Stage 1: builder
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build setuptools wheel
COPY pyproject.toml ./
COPY sovereign_os sovereign_os
COPY charter.example.yaml ./
RUN pip wheel --no-deps --wheel-dir /wheels .

# Stage 2: runtime
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Install runtime deps (optional: redis for health check)
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && pip install --no-cache-dir "prometheus-client>=0.19" "redis>=5.0" \
    && rm -rf /wheels

COPY --from=builder /build/sovereign_os sovereign_os
COPY --from=builder /build/charter.example.yaml charter.example.yaml
COPY pyproject.toml ./

# Persist ledger + ChromaDB data
VOLUME ["/app/data"]
ENV SOVEREIGN_DATA_DIR=/app/data

# Prometheus metrics port
EXPOSE 9464
# Health API port
EXPOSE 8080

# Default: run TUI (override in compose for health-only or custom cmd)
CMD ["python", "-m", "sovereign_os.ui.app"]
