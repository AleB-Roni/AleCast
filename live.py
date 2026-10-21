"""Live-Bild des Handys für die Web-UI: scrcpy-Server (eigene Kennung) -> ffmpeg -> MJPEG.
Läuft nur, solange jemand zuschaut, und mehrere Zuschauer teilen sich einen Datenstrom."""
import collections
import os
import socket
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
        self._server_log = collections.deque(maxlen=30)
        self._last_frame_at = 0.0
        self.mode = "scrcpy"
        self.error = ""
        threading.Thread(target=self._janitor, daemon=True).start()

    # ---------- öffentlich ----------

    def status(self):
        return {"available": device_connected(), "running": self._running, "mode": self.mode,
                "clients": self._clients, "error": self.error,
                "last_frame_age": round(time.time() - self._last_frame_at, 1) if self._last_frame_at else None,
                "server_log": list(self._server_log)[-8:]}

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

    def _log(self, msg):
        print(f"[AleCast-Live] {msg}", flush=True)

    def _publish(self, frame):
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._last_frame_at = time.time()
            self._cond.notify_all()

    def _run(self):
        failures = 0
        while self._running:
            if not device_connected():
                self.error = "Handy nicht per USB verbunden"
                time.sleep(3)
                continue
            if failures >= 1:
                # scrcpy klappt nicht: ersatzweise Bildschirmfotos (ca. 1 Bild/s)
                self.mode = "screenshot"
                self._log("Live per scrcpy nicht möglich – weiche auf Bildschirmfotos aus")
                self._screenshot_loop()
                break
            self.mode = "scrcpy"
            try:
                got = self._stream_once()
                failures = 0 if got else failures + 1
            except Exception as e:  # noqa: BLE001
                failures += 1
                self.error = str(e)
                self._log(f"Fehler: {e}")
            finally:
                self._kill()
                self._stop_device_side()
            if self._running:
                time.sleep(2)
        self._kill()
        self._stop_device_side()
        self.mode = "scrcpy"
        with self._cond:
            self._frame = None
            self._cond.notify_all()

    def _read_server_log(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self._server_log.append(line)
                    self._log("Handy: " + line)
        except (ValueError, OSError):
            pass

    def _connect(self, deadline):
        """Verbindet sich mit dem scrcpy-Server. Solange er noch startet, schließt adb die
        Verbindung sofort wieder – dann erneut versuchen, statt den Server neu zu starten."""
        while time.time() < deadline and self._running:
            if self._server and self._server.poll() is not None:
                return None, b""
            try:
                sock = socket.create_connection(("127.0.0.1", PORT), timeout=2)
                sock.settimeout(4)
                try:
                    first = sock.recv(65536)
                except socket.timeout:
                    first = b"\x00"[:0]
                    return sock, first  # verbunden, Bild kommt gleich
                if first:
                    return sock, first
                sock.close()
            except OSError:
                pass
            time.sleep(0.5)
        return None, b""

    def _stream_once(self):
        """True, wenn mindestens ein Bild kam."""
        listing = _adb("shell", "ls", DEVICE_JAR, timeout=5)
        if DEVICE_JAR not in listing or "No such file" in listing:
            _adb("push", LOCAL_JAR, DEVICE_JAR, timeout=30)
        self._stop_device_side()
        _adb("forward", f"tcp:{PORT}", f"localabstract:scrcpy_{SCID}")
        self._server_log.clear()
        self._server = subprocess.Popen(
            ["adb", "shell", f"CLASSPATH={DEVICE_JAR}", "app_process", "/", "com.genymobile.scrcpy.Server",
             SCRCPY_VERSION, f"scid={SCID}", "tunnel_forward=true", "audio=false", "control=false",
             "cleanup=false", "raw_stream=true", "video_codec=h264", f"max_size={MAX_SIZE}",
             "max_fps=15", "video_bit_rate=3000000", "log_level=info"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._read_server_log, args=(self._server,), daemon=True).start()

        # Sofort ein Standbild zeigen, bis das Video läuft
        threading.Thread(target=self._one_screenshot, daemon=True).start()
        sock, first = self._connect(time.time() + 12)
        if sock is None:
            log = " | ".join(list(self._server_log)[-3:]) or "keine Antwort"
            raise RuntimeError(f"scrcpy-Server startet nicht: {log}")

        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "65536", "-analyzeduration", "500000",
             "-f", "h264", "-i", "-", "-vf", f"fps={FPS}", "-q:v", "6", "-f", "mjpeg", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ff = self._ffmpeg

        def pump():
            try:
                if first:
                    ff.stdin.write(first)
                sock.settimeout(None)
                while self._running:
                    data = sock.recv(65536)
                    if not data:
                        break
                    ff.stdin.write(data)
            except OSError:
                pass
            finally:
                try:
                    sock.close()
                    ff.stdin.close()
                except OSError:
                    pass
        threading.Thread(target=pump, daemon=True).start()

        self.error = ""
        got = False
        started = time.time()

        def watchdog():
            # Hängt der Strom (kein Bild seit 20 s), Verbindung kappen -> Neustart
            while ff.poll() is None and self._running:
                time.sleep(2)
                if time.time() - max(started, self._last_frame_at) > 20:
                    self._log("Kein Bild seit 20 s – starte Live-Strom neu")
                    try:
                        sock.close()
                    except OSError:
                        pass
                    ff.kill()
                    break
        threading.Thread(target=watchdog, daemon=True).start()
        buf = b""
        out = ff.stdout
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
                if not got:
                    self._log(f"Live-Bild läuft (erstes Bild nach {time.time() - started:.1f} s)")
                got = True
                self._publish(frame)
            if len(buf) > 8_000_000:  # Schutz gegen Müll im Datenstrom
                buf = b""
        if not got:
            log = " | ".join(list(self._server_log)[-3:])
            self.error = "scrcpy liefert keine Bilder" + (f": {log}" if log else "")
        return got

    def _grab_jpeg(self):
        png = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=8).stdout
        if png[:4] != b"\x89PNG":
            return None
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "png_pipe", "-i", "-",
             "-vf", f"scale='min({MAX_SIZE},iw)':-2", "-q:v", "6", "-f", "mjpeg", "-"],
            input=png, capture_output=True, timeout=8).stdout or None

    def _one_screenshot(self):
        try:
            started = time.time()
            jpg = self._grab_jpeg()
            if jpg and self._last_frame_at < started:
                self._publish(jpg)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _screenshot_loop(self):
        while self._running and device_connected():
            t0 = time.time()
            try:
                jpg = self._grab_jpeg()
                if jpg:
                    self._publish(jpg)
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))


stream = LiveStream()
