FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/config \
    STALLARR_DB_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV PYTHONPATH=/app/src

# /config holds config.json and may be a network share; /data holds the sqlite
# database and must be local disk (SMB/NFS cannot do the locking SQLite needs).
VOLUME ["/data"]

RUN addgroup -g 1000 stallarr && adduser -D -u 1000 -G stallarr stallarr \
    && mkdir -p /config /data && chown -R stallarr:stallarr /config /data /app
USER stallarr

EXPOSE 8383

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8383/health', timeout=5).status==200 else 1)"

CMD ["python", "-m", "stallarr"]
