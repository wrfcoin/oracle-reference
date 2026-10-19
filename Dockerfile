# oracle-reference — CPU-only reference-verification service.
# Local builds only (operator testnet stack); NEVER pushed to a registry.
# Expected image size ~2-2.5 GB (CPU torch + xarray/zarr/cfgrib stack).
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# CPU torch pinned first so earth2studio never resolves a CUDA build.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY pyproject.toml .

ENV PORT=8010 \
    EARTH2STUDIO_CACHE=/data/cache \
    CACHE_MAX_GB=10

VOLUME ["/data/cache"]
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -f http://localhost:8010/healthz || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
