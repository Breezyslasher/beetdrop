# Beetdrop
#
#   docker build -t beetdrop .
#   docker run -d --restart unless-stopped -p 8090:8090 \
#     -v /path/to/appdata:/config -v /path/to/inbox:/inbox \
#     -e PUID=1000 -e PGID=1000 beetdrop
#
# Single-stage on python slim; ffmpeg from apt. yt-dlp installs unpinned
# because it ages against YouTube changes on a scale of weeks - set
# BEETDROP_SELFUPDATE=1 to refresh it at container start.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY beetdrop/ beetdrop/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && groupadd -g 1000 beetdrop \
    && useradd -u 1000 -g beetdrop -m beetdrop \
    && mkdir -p /config /inbox /music

ENV BEETDROP_CONFIG=/config \
    INBOX_PATH=/inbox \
    MUSIC_PATH=/music \
    PYTHONUNBUFFERED=1

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
