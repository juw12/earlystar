FROM python:3.12-slim

# EarlyStar uses only the Python standard library — nothing to pip install.
WORKDIR /app
COPY collector.py markets.py auth.py server.py export_static.py index.html manifest.webmanifest sw.js ./
COPY icons ./icons

ENV EARLYSTAR_DATA_DIR=/data \
    PORT=8765 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765

RUN useradd --create-home earlystar && mkdir -p /data && chown earlystar /data
USER earlystar

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'])"
CMD ["python", "server.py", "--public"]
