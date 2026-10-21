#!/bin/bash
# AleCast – streamt den Bildschirm eines per USB angeschlossenen Android-Handys
# über den scrcpy-Server als RTSP (Push an go2rtc/Frigate oder einen RTSP-Server).
set -u

ALECAST_LOG="${ALECAST_LOG:-/tmp/alecast.log}"
export ALECAST_LOG
: > "${ALECAST_LOG}"
log() {
  local line="[AleCast $(date '+%F %T')] $*"
  echo "${line}"
  echo "${line}" >> "${ALECAST_LOG}"
  # Logdatei klein halten
  if [ "$(wc -l < "${ALECAST_LOG}")" -gt 2000 ]; then
    tail -n 500 "${ALECAST_LOG}" > "${ALECAST_LOG}.tmp" && mv "${ALECAST_LOG}.tmp" "${ALECAST_LOG}"
  fi
}

STREAMING=1
if [ -z "${RTSP_URL:-}" ]; then
  STREAMING=0
fi

[ -n "${ADB_SERIAL}" ] && export ANDROID_SERIAL="${ADB_SERIAL}"

SERVER_JAR=/data/local/tmp/alecast-scrcpy-server.jar
LOCAL_PORT=27183
SERVER_PID=""
TAP_PID=""
CENTER_PID=""
CENTER_X="${CENTER_X:-}"
CENTER_Y="${CENTER_Y:-}"

cleanup() {
  log "Beende ..."
  [ -n "${TAP_PID}" ] && kill "${TAP_PID}" 2>/dev/null
  [ -n "${CENTER_PID}" ] && kill "${CENTER_PID}" 2>/dev/null
  [ -n "${WEBUI_PID:-}" ] && kill "${WEBUI_PID}" 2>/dev/null
  [ -n "${WATCHDOG_PID:-}" ] && kill "${WATCHDOG_PID}" 2>/dev/null
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

# ---------- AleCorder (Aufnahme-App auf dem Handy) ----------
ALECORDER_PKG=de.aleb.alecorder

alecorder_installed() {
  adb shell pm list packages "${ALECORDER_PKG}" 2>/dev/null | tr -d '\r' | grep -qx "package:${ALECORDER_PKG}"
}

# Bildschirmaufnahme ohne Rückfrage erlauben, Hintergrundstart und Akku-Ausnahme
alecorder_permissions() {
  adb shell appops set "${ALECORDER_PKG}" PROJECT_MEDIA allow >/dev/null 2>&1
  adb shell appops set "${ALECORDER_PKG}" SYSTEM_ALERT_WINDOW allow >/dev/null 2>&1
  adb shell dumpsys deviceidle whitelist "+${ALECORDER_PKG}" >/dev/null 2>&1
  # Bedienungshilfe "AleCorder Tipp-Automatik" einschalten (für die Tipp-Punkte)
  local svc="${ALECORDER_PKG}/${ALECORDER_PKG}.TapService" cur
  cur="$(adb shell settings get secure enabled_accessibility_services 2>/dev/null | tr -d '\r')"
  case "${cur}" in
    *"${ALECORDER_PKG}/"*) ;;
    ""|null) adb shell settings put secure enabled_accessibility_services "${svc}" >/dev/null 2>&1 ;;
    *) adb shell settings put secure enabled_accessibility_services "${cur}:${svc}" >/dev/null 2>&1 ;;
  esac
  adb shell settings put secure accessibility_enabled 1 >/dev/null 2>&1
}

# Startet AleCorder neu, wenn er aufnehmen soll, aber nicht läuft (z. B. nach Neustart oder Absturz)
alecorder_watchdog() {
  [ "${ALECORDER_WATCHDOG:-1}" = "1" ] || return
  local announced=0
  while true; do
    sleep 60
    [ "$(adb get-state 2>/dev/null)" = "device" ] || continue
    alecorder_installed || continue
    if [ "${announced}" = "0" ]; then
      log "AleCorder gefunden – Aufnahme-Erlaubnis gesetzt, Watchdog aktiv"
      announced=1
    fi
    alecorder_permissions
    want="$(adb shell cat "/sdcard/Android/data/${ALECORDER_PKG}/files/should_run" 2>/dev/null | tr -d '\r')"
    [ "${want}" = "1" ] || continue
    if ! adb shell dumpsys activity services "${ALECORDER_PKG}" 2>/dev/null | grep -q "RecorderService"; then
      log "AleCorder läuft nicht – starte Aufnahme neu"
      adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
      adb shell am start -n "${ALECORDER_PKG}/.ProjectionRequestActivity" --ez auto true >/dev/null 2>&1
    fi
  done
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

# Bildschirmmitte ermitteln (aktuelle Ausrichtung, z. B. Querformat)
screen_center() {
  local xml bounds w h size
  xml="$(adb exec-out uiautomator dump /dev/tty 2>/dev/null)"
  bounds="$(printf '%s' "${xml}" | grep -oE 'bounds="\[0,0\]\[[0-9]+,[0-9]+\]"' | head -n1 \
    | sed -nE 's/.*\]\[([0-9]+),([0-9]+)\].*/\1 \2/p')"
  if [ -n "${bounds}" ]; then
    read -r w h <<< "${bounds}"
  else
    size="$(adb shell wm size | tr -d '\r' | grep -oE '[0-9]+x[0-9]+' | tail -n1)"
    w="${size%x*}"; h="${size#*x}"
    [ -z "${w}" ] && return 1
  fi
  echo "$(( w / 2 )) $(( h / 2 ))"
}

# Tippt regelmäßig in die Bildschirmmitte (z. B. "Aktualisieren" bei hängendem Feed)
center_tap_loop() {
  [ "${CENTER_TAP_INTERVAL:-0}" -gt 0 ] 2>/dev/null || return
  log "Mitte-Tippen aktiv (alle ${CENTER_TAP_INTERVAL}s)"
  while true; do
    sleep "${CENTER_TAP_INTERVAL}"
    local c x y
    if [ -n "${CENTER_X}" ] && [ -n "${CENTER_Y}" ]; then
      x="${CENTER_X}"; y="${CENTER_Y}"
    else
      c="$(screen_center)" || { log "Mitte-Tippen: Bildschirmgröße unbekannt"; continue; }
      read -r x y <<< "${c}"
    fi
    adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
    adb shell input tap "${x}" "${y}" >/dev/null 2>&1 && log "Mitte-Tippen auf ${x},${y}"
  done
}

# Einstellungen aus der Web-UI (Aufnahmebereich, Auflösung) abfragen
setting() { python3 /opt/alecast/webui.py "--get-$1" 2>/dev/null; }

start_server() {
  local max_size
  max_size="$(setting max-size)"
  [ -z "${max_size}" ] && max_size="${MAX_SIZE}"
  adb shell pkill -f com.genymobile.scrcpy >/dev/null 2>&1
  adb forward --remove-all >/dev/null 2>&1
  adb forward "tcp:${LOCAL_PORT}" localabstract:scrcpy >/dev/null || return 1
  adb shell CLASSPATH="${SERVER_JAR}" app_process / com.genymobile.scrcpy.Server "${SCRCPY_VERSION}" \
    tunnel_forward=true audio=false control=false cleanup=false raw_stream=true \
    video_codec=h264 max_size="${max_size}" max_fps="${MAX_FPS}" \
    video_bit_rate="${VIDEO_BIT_RATE}" &
  SERVER_PID=$!
  sleep 2
}

run_ffmpeg() {
  local in_args=(-hide_banner -loglevel warning
                 -fflags +genpts+nobuffer -use_wallclock_as_timestamps 1
                 -f h264 -i "tcp://127.0.0.1:${LOCAL_PORT}")
  local out_args extra_vf vf
  extra_vf="$(setting vf)"
  vf="fps=${OUT_FPS}"
  if [ -n "${extra_vf}" ]; then
    vf="${vf},${extra_vf}"
    log "Aufnahmebereich/Skalierung aktiv: ${extra_vf}"
  fi
  if [ "${TRANSCODE}" = "1" ] || [ -n "${extra_vf}" ]; then
    out_args=(-vf "${vf}" -c:v libx264 -preset veryfast -tune zerolatency
              -pix_fmt yuv420p -g "$(( OUT_FPS * 2 ))" -b:v "${VIDEO_BIT_RATE}")
  else
    out_args=(-c copy)
  fi
  ffmpeg "${in_args[@]}" -an "${out_args[@]}" -rtsp_transport tcp -f rtsp "${RTSP_URL}"
}

if [ "${STREAMING}" = "1" ]; then
  log "Start – Ziel: ${RTSP_URL}"
else
  log "Start ohne RTSP-Streaming (nur Web-UI, Aufnahme-Server und Auto-Tap)"
fi
mkdir -p "${RECORDINGS_DIR:-/recordings}" 2>/dev/null

WEBUI_PID=""
if [ "${WEBUI_ENABLED:-1}" = "1" ]; then
  python3 /opt/alecast/webui.py &
  WEBUI_PID=$!
  log "Web-UI erreichbar auf Port ${WEBUI_PORT:-8080}"
fi
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

  if [ -z "${CENTER_PID}" ] || ! kill -0 "${CENTER_PID}" 2>/dev/null; then
    center_tap_loop &
    CENTER_PID=$!
  fi

  if alecorder_installed; then alecorder_permissions; fi
  if [ -z "${WATCHDOG_PID:-}" ] || ! kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
    alecorder_watchdog &
    WATCHDOG_PID=$!
  fi

  if [ "${STREAMING}" = "0" ]; then
    # Kein Streaming: nur warten, solange das Handy verbunden ist
    while [ "$(adb get-state 2>/dev/null)" = "device" ]; do sleep 10; done
    log "Handy getrennt"
    continue
  fi

  start_server || { log "adb forward fehlgeschlagen"; sleep 5; continue; }
  log "Stream läuft ..."
  run_ffmpeg
  log "Stream unterbrochen – starte neu in 3s"
  kill "${SERVER_PID}" 2>/dev/null
  SERVER_PID=""
  sleep 3
done
