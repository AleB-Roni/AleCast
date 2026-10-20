# AleCast – Android-Bildschirm (per USB) als RTSP-Stream, z. B. für Frigate
FROM debian:bookworm-slim

ARG SCRCPY_VERSION=4.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends adb ffmpeg ca-certificates curl procps \
 && rm -rf /var/lib/apt/lists/*

# Offizieller scrcpy-Server (läuft auf dem Handy, liefert rohen H.264-Stream)
RUN curl -fsSL -o /opt/scrcpy-server.jar \
      "https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/scrcpy-server-v${SCRCPY_VERSION}"

ENV SCRCPY_VERSION=${SCRCPY_VERSION} \
    RTSP_URL="" \
    ADB_SERIAL="" \
    MAX_SIZE=1280 \
    MAX_FPS=15 \
    VIDEO_BIT_RATE=4000000 \
    TRANSCODE=1 \
    OUT_FPS=10 \
    TAP_TEXT="Weiter ansehen|Weiterschauen|Weiter schauen|Continue watching|Continue" \
    TAP_INTERVAL=15 \
    KEEP_AWAKE=1

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
