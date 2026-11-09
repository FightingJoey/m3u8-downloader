# Pornhub + m3u8-downloader bridge (ph_server)
# Build: docker build -t m3u8-downloader-ph .
# Run:   docker run --rm -p 8765:8765 m3u8-downloader-ph

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PH_HOST=0.0.0.0 \
    PH_PORT=8765

WORKDIR /app

# Static UI + bridge (stdlib only; Playwright optional and not installed)
COPY ph_server.py download_ph_1080.py ./
COPY index.html index-en.html mitm.html ./
COPY vue.js mux-mp4.js aes-decryptor.js StreamSaver.js serviceWorker.js m3u8-downloader.user.js ./
COPY imgs ./imgs
COPY README.md README-EN.md README-PH.md ./

RUN mkdir -p /app/cache /app/_sessions \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && chown -R app:app /app

USER app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"

CMD ["python", "ph_server.py"]
