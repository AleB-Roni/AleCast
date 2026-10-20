#!/bin/bash
# AleCast – streamt den Bildschirm eines per USB angeschlossenen Android-Handys
# über den scrcpy-Server als RTSP (Push an go2rtc/Frigate oder einen RTSP-Server).
set -u

log() { echo "[AleCast $(date '+%F %T')] $*"; }

if [ -z "${RTSP_URL}" ]; then
  log "FEHLER: RTSP_URL ist nicht gesetzt (z. B. rtsp://192.168.1.10:8554/tapo_app)"
  exit 1
fi

[ -n "${ADB_SERIAL}" ] && export ANDROID_SERIAL="${ADB_SERIAL}"

SERVER_JAR=/data/local/tmp/alecast-scrcpy-server.jar
LOCAL_PORT=27183
SERVER_PID=""
TAP_PID=""

cleanup() {
  log "Beende ..."
  [ -n "${TAP_PID}" ] && kill "${TAP_PID}" 2>/dev/null
  [ -n "${SERVER_PID}" ] && kill "${SERVER_PID}" 2>/dev/null
  adb shell pkill -f com.genymobile.scrcpy >/dev/null 2>&1
  adb forward --remove-all >/dev/null 2>&1
  exit 0
}
trap cleanup SIGTERM SIGINT

wait_for_device() {
  adb start-server >/dev/null 2>&1
  until [ "$(adb get-state 2>/dev/null)" = "device" ]; do
    state="$(adb devices | sed -n '2p' | awk '{print $2}')"
    if [ "${state}" = "unauthorized" ]; then
      log "Handy fragt nach USB-Debugging-Freigabe – auf dem Handy 'Immer zulassen' antippen."
    else
      log "Warte auf Handy per USB (USB-Debugging an? Kabel steckt?) ..."
    fi
    sleep 5
  done
  log "Handy verbunden: $(adb shell getprop ro.product.model | tr -d '\r')"
}

prepare_device() {
  if [ "${KEEP_AWAKE}" = "1" ]; then
    adb shell svc power stayon usb >/dev/null 2>&1
    adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
  fi
  adb push /opt/scrcpy-server.jar "${SERVER_JAR}" >/dev/null || return 1
}

# Tippt den "Weiterschauen"-Dialog der Tapo-App automatisch weg
autotap_loop() {
  [ -z "${TAP_TEXT}" ] && return
  log "Auto-Tap aktiv (Text: ${TAP_TEXT}, alle ${TAP_INTERVAL}s)"
  while true; do
    sleep "${TAP_INTERVAL}"
    xml="$(adb exec-out uiautomator dump /dev/tty 2>/dev/null)" || continue
    node="$(printf '%s' "${xml}" | tr '>' '\n' \
      | grep -iE "(text|content-desc)=\"(${TAP_TEXT})\"" | head -n1)"
    [ -z "${node}" ] && continue
    bounds="$(printf '%s' "${node}" \
      | sed -nE 's/.*bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]".*/\1 \2 \3 \4/p')"
    [ -z "${bounds}" ] && continue
    read -r x1 y1 x2 y2 <<< "${bounds}"
    x=$(( (x1 + x2) / 2 )); y=$(( (y1 + y2) / 2 ))
    log "Dialog gefunden – tippe auf ${x},${y}"
    adb shell input tap "${x}" "${y}" >/dev/null 2>&1
  done
}

start_server() {
  adb shell pkill -f com.genymobile.scrcpy >/dev/null 2>&1
  adb forward --remove-all >/dev/null 2>&1
  adb forward "tcp:${LOCAL_PORT}" localabstract:scrcpy >/dev/null || return 1
  adb shell CLASSPATH="${SERVER_JAR}" app_process / com.genymobile.scrcpy.Server "${SCRCPY_VERSION}" \
    tunnel_forward=true audio=false control=false cleanup=false raw_stream=true \
    video_codec=h264 max_size="${MAX_SIZE}" max_fps="${MAX_FPS}" \
    video_bit_rate="${VIDEO_BIT_RATE}" &
  SERVER_PID=$!
  sleep 2
}

run_ffmpeg() {
  local in_args=(-hide_banner -loglevel warning
                 -fflags +genpts+nobuffer -use_wallclock_as_timestamps 1
                 -f h264 -i "tcp://127.0.0.1:${LOCAL_PORT}")
  local out_args
  if [ "${TRANSCODE}" = "1" ]; then
    out_args=(-vf "fps=${OUT_FPS}" -c:v libx264 -preset veryfast -tune zerolatency
              -pix_fmt yuv420p -g "$(( OUT_FPS * 2 ))" -b:v "${VIDEO_BIT_RATE}")
  else
    out_args=(-c copy)
  fi
  ffmpeg "${in_args[@]}" -an "${out_args[@]}" -rtsp_transport tcp -f rtsp "${RTSP_URL}"
}

log "Start – Ziel: ${RTSP_URL}"
while true; do
  wait_for_device
  if ! prepare_device; then
    log "Konnte scrcpy-Server nicht aufs Handy kopieren – neuer Versuch in 5s"
    sleep 5; continue
  fi

  if [ -z "${TAP_PID}" ] || ! kill -0 "${TAP_PID}" 2>/dev/null; then
    autotap_loop &
    TAP_PID=$!
  fi

  start_server || { log "adb forward fehlgeschlagen"; sleep 5; continue; }
  log "Stream läuft ..."
  run_ffmpeg
  log "Stream unterbrochen – starte neu in 3s"
  kill "${SERVER_PID}" 2>/dev/null
  SERVER_PID=""
  sleep 3
done
