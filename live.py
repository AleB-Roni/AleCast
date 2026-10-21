"""Live-Bild des Handys für die Web-UI: scrcpy-Server (eigene Kennung) -> ffmpeg -> MJPEG.
Läuft nur, solange jemand zuschaut, und mehrere Zuschauer teilen sich einen Datenstrom."""
import os
import subprocess
import threading
import time

SCRCPY_VERSION = os.environ.get("SCRCPY_VERSION", "4.0")
LOCAL_JAR = "/opt/scrcpy-server.jar"
DEVICE_JAR = "/data/local/tmp/alecast-scrcpy-server.jar"
SCID = "0a11fe57"          # eigene Kennung, unabhängig vom RTSP-Stream
PORT = 27190
MAX_SIZE = int(os.environ.get("LIVE_MAX_SIZE", "1280"))
FPS = int(os.environ.get("LIVE_FPS", "8"))
IDLE_STOP_S = 15


def _adb(*args, timeout=10):
    try:
        return subprocess.run(["adb", *args], capture_output=True, timeout=timeout, text=True).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def device_connected():
    return _adb("get-state", timeout=4).strip() == "device"


class LiveStream:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._clients = 0
        self._last_client = 0.0
        self._running = False
        self._server = None
        self._ffmpeg = None
        self._thread = None
        self.error = ""
        threading.Thread(target=self._janitor, daemon=True).start()

    # ---------- öffentlich ----------

    def status(self):
        return {"available": device_connected(), "running": self._running,
                "clients": self._clients, "error": self.error}

    def acquire(self):
        with self._lock:
            self._clients += 1
            self._running = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def release(self):
        with self._lock:
            self._clients = max(0, self._clients - 1)
            self._last_client = time.time()

    def frames(self, timeout=12):
        """Liefert neue JPEG-Bilder, bis länger als `timeout` Sekunden keins kommt."""
        with self._cond:
            seen = self._seq
            first = self._frame
        if first is not None:
            yield first  # sofort ein Bild zeigen, falls der Strom schon läuft
        while True:
            with self._cond:
                if self._seq == seen:
                    self._cond.wait(timeout)
                if self._seq == seen:
                    return
                seen = self._seq
                frame = self._frame
            if frame is None:
                return
            yield frame

    def latest(self, wait=6):
        """Ein einzelnes aktuelles Bild (startet den Strom kurz, falls nötig)."""
        self.acquire()
        try:
            for f in self.frames(timeout=wait):
                return f
        finally:
            self.release()
        return None

    # ---------- intern ----------

    def _janitor(self):
        while True:
            time.sleep(3)
            with self._lock:
                idle = self._clients == 0 and time.time() - self._last_client > IDLE_STOP_S
                if self._running and idle:
                    self._running = False
            if not self._running and (self._thread is None or not self._thread.is_alive()):
                self._kill()

    def _kill(self):
        for p in (self._ffmpeg, self._server):
            if p and p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
        self._ffmpeg = self._server = None

    def _stop_device_side(self):
        _adb("shell", "pkill", "-f", f"scid={SCID}", timeout=5)
        _adb("forward", "--remove", f"tcp:{PORT}", timeout=5)

    def _run(self):
        while self._running:
            if not device_connected():
                self.error = "Handy nicht per USB verbunden"
                time.sleep(3)
                continue
            try:
                self._stream_once()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
            finally:
                self._kill()
                self._stop_device_side()
            if self._running:
                time.sleep(2)
        self._kill()
        self._stop_device_side()
        with self._cond:
            self._frame = None
            self._cond.notify_all()

    def _stream_once(self):
        if DEVICE_JAR not in _adb("shell", "ls", DEVICE_JAR, timeout=5):
            _adb("push", LOCAL_JAR, DEVICE_JAR, timeout=30)
        self._stop_device_side()
        _adb("forward", f"tcp:{PORT}", f"localabstract:scrcpy_{SCID}")
        self._server = subprocess.Popen(
            ["adb", "shell", f"CLASSPATH={DEVICE_JAR}", "app_process", "/", "com.genymobile.scrcpy.Server",
             SCRCPY_VERSION, f"scid={SCID}", "tunnel_forward=true", "audio=false", "control=false",
             "cleanup=false", "raw_stream=true", "video_codec=h264", f"max_size={MAX_SIZE}",
             "max_fps=15", "video_bit_rate=3000000", "log_level=error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "32768", "-analyzeduration", "0",
             "-f", "h264", "-i", f"tcp://127.0.0.1:{PORT}",
             "-vf", f"fps={FPS}", "-q:v", "6", "-f", "mjpeg", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.error = ""
        buf = b""
        out = self._ffmpeg.stdout
        while self._running:
            chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    if start > 0:
                        buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._cond.notify_all()
            if len(buf) > 8_000_000:  # Schutz gegen Müll im Datenstrom
                buf = b""


stream = LiveStream()
