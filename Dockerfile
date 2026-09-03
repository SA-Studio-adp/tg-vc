# Single-container image for Render: runs the Telegram bot (+ web
# dashboard) AND the bgutil PO-token HTTP server side by side, since
# Render's free/starter web-service tier only runs one container with
# no sidecars. start.sh launches the PO token server in the background,
# then execs the bot as PID 1.
FROM python:3.12-slim

# --- system deps ---
# ffmpeg: required to transcode/pipe media into the RTMP push.
# aria2: multi-connection external downloader yt-dlp can hand off to
#   for faster single-file downloads (see ARIA2_ENABLED in
#   downloader.py) — confirmed via a real test download at ~4x the
#   single-connection speed. Optional at runtime (auto-detected), but
#   included here so it's actually present without extra setup.
# nodejs/npm: required to build+run the bgutil PO-token server (it's a
#   TypeScript/Node HTTP service, not a Python package, despite the pip
#   plugin sharing its name).
# git: needed to clone the bgutil server source at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        git \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- bgutil PO-token server ---
# Pinned to a specific release tag (matching the bgutil-ytdlp-pot-provider
# pip plugin version in requirements.txt) rather than tracking a branch,
# so a Render rebuild doesn't silently pick up a breaking server change.
# If you bump the pip package version later, bump this tag to match.
ARG BGUTIL_VERSION=1.3.2
RUN git clone --single-branch --branch ${BGUTIL_VERSION} --depth 1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-pot \
    && cd /opt/bgutil-pot/server \
    && npm ci \
    && npx tsc

# --- python app ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Talk to the PO-token server over localhost — start.sh runs both
# processes in this same container.
ENV POT_PROVIDER_BASE_URL=http://127.0.0.1:4416

RUN chmod +x start.sh

# Render sets $PORT itself and routes to whatever the container binds;
# this EXPOSE is documentation only (Render ignores it, but it's correct
# for running the image anywhere else too, e.g. `docker run -p 10000:10000`).
EXPOSE 10000

CMD ["./start.sh"]
