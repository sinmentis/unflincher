# Containerfile
FROM docker.io/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DIARY_DB=/data/diary.db
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["tini", "--"]
# --workers 1 is mandatory: single SQLite writer + single in-process batch worker (see
# Global Constraints and technical design §7.6 point 1). Never change this.
CMD ["uvicorn", "diary.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-graceful-shutdown", "30"]
