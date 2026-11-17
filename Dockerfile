FROM python:3.13-alpine

LABEL org.opencontainers.image.title="shelfcull" \
      org.opencontainers.image.description="Click through your Radarr / Sonarr library, see who watched what in Plex, delete what you do not want" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data PORT=8765

RUN adduser -D -H -u 1000 shelfcull && mkdir /data && chown shelfcull /data
WORKDIR /app
COPY shelfcull/ /app/shelfcull/

USER shelfcull
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/healthz',timeout=4)" || exit 1

CMD ["python", "-m", "shelfcull"]
