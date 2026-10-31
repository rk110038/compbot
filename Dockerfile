# Chromium + all its system libraries are baked into the official Playwright image (version must match requirements.txt).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    TZ=Europe/London \
    HOME=/home/compbot

WORKDIR /app
COPY requirements.txt .
# Override for builds behind a corporate package mirror, e.g. --build-arg PIP_INDEX_URL=https://mirror/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
RUN pip install --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY app ./app

# Default to UID/GID 568 = TrueNAS SCALE's built-in "apps" user, so a dataset owned by apps:apps just works.
RUN groupadd -g 568 compbot \
 && useradd -u 568 -g 568 -m -d /home/compbot -s /usr/sbin/nologin compbot \
 && mkdir -p /data \
 && chown -R 568:568 /data /home/compbot \
 && chmod 777 /home/compbot
USER 568:568

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
