FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    SMTP_RELAY_HOSTNAME=0.0.0.0 \
    SMTP_RELAY_PORT=2525

WORKDIR /usr/src/app

# Every dependency ships a wheel for 3.14, so no compiler is needed.
# Hash-checked install: requirements.txt is generated from requirements.in.
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt

RUN groupadd --system --gid 10001 relay \
    && useradd --system --uid 10001 --gid relay --no-create-home --shell /usr/sbin/nologin relay \
    && mkdir -p /data \
    && chown relay:relay /data

COPY app/ .

USER relay
VOLUME ["/data"]
EXPOSE 2525 8080

# Healthy once the SMTP greeting (220) comes back.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,socket; s=socket.create_connection(('127.0.0.1', int(os.environ.get('SMTP_RELAY_PORT', '2525'))), 5); assert s.recv(3) == b'220'"]

CMD ["python", "main.py"]
