# Containerfile
FROM docker.io/library/python:3.12-slim

ARG UNFLINCHER_REVISION=development
ARG UNFLINCHER_VERSION=development
ARG UNFLINCHER_BUILD_CREATED=1970-01-01T00:00:00Z

LABEL org.opencontainers.image.title="Unflincher" \
      org.opencontainers.image.description="Evidence-grounded AI reflection partner" \
      org.opencontainers.image.source="https://github.com/sinmentis/unflincher" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.revision="${UNFLINCHER_REVISION}" \
      org.opencontainers.image.version="${UNFLINCHER_VERSION}" \
      org.opencontainers.image.created="${UNFLINCHER_BUILD_CREATED}"

RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Bake the Copilot CLI runtime into the image at build time so a fresh container doesn't fetch
# it from GitHub Releases on first request (avoids that network dependency + latency spike),
# mirroring how this repo vendors htmx.min.js instead of hitting a CDN at runtime.
RUN python -m copilot download-runtime

ENV UNFLINCHER_DB=/data/unflincher.db \
    UNFLINCHER_REVISION=${UNFLINCHER_REVISION} \
    UNFLINCHER_VERSION=${UNFLINCHER_VERSION}
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["tini", "--"]
# --workers 1 is mandatory: single SQLite writer + single in-process batch worker (see
# Global Constraints and technical design §7.6 point 1). Never change this.
CMD ["uvicorn", "unflincher.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-graceful-shutdown", "30"]
