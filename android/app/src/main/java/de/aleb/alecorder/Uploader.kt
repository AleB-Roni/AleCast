package de.aleb.alecorder

import android.content.Context
import android.util.Base64
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlin.concurrent.thread
import kotlin.math.min

/**
 * Lädt fertige Segmente der Reihe nach (ältestes zuerst) zum AleCast-Server hoch.
 * Ist der Server nicht erreichbar, bleiben die Dateien liegen und werden später nachgeholt.
 */
class Uploader private constructor(context: Context, private val dir: File) {
    private val ctx = context.applicationContext
    private val lock = Object()
    @Volatile private var running = true
    private val worker = thread(name = "AleCorder-Upload") { loop() }

    fun wake() = synchronized(lock) { lock.notifyAll() }

    private fun waitFor(ms: Long) = synchronized(lock) {
        if (running) lock.wait(ms)
    }

    private fun pending(): List<File> =
        dir.listFiles { f -> f.isFile && f.name.endsWith(".mp4") }?.sortedBy { it.name } ?: emptyList()

    private fun loop() {
        var backoff = 5_000L
        while (running) {
            enforceLimit()
            val files = pending()
            RecorderState.queueCount = files.size
            RecorderState.queueBytes = files.sumOf { it.length() }
            if (files.isEmpty()) {
                waitFor(30_000)
                continue
            }
            val f = files.first()
            val ok = try {
                upload(f)
            } catch (e: Exception) {
                RecorderState.lastError = "Upload: ${e.javaClass.simpleName} ${e.message ?: ""}".trim()
                false
            }
            if (ok) {
                f.delete()
                backoff = 5_000L
                RecorderState.uploadedCount++
                RecorderState.lastUploadAt = System.currentTimeMillis()
                RecorderState.lastError = null
            } else {
                waitFor(backoff)
                backoff = min(backoff * 2, 60_000L)
            }
        }
    }

    private fun upload(f: File): Boolean {
        val parts = f.name.removeSuffix(".mp4").split("_")
        val start = parts.getOrNull(0)?.toLongOrNull()
        val dur = parts.getOrNull(1)?.toLongOrNull()
        if (start == null || dur == null) {
            f.delete()
            return false
        }
        val prefs = Prefs(ctx)
        var unreachable = ""
        for ((name, base) in routes(prefs)) {
            val (code, msg) = send(f, base, prefs, start, dur, if (name == USB) 2_000 else 10_000)
            when {
                code in 200..299 -> {
                    RecorderState.lastUploadVia = name
                    RecorderState.usbAvailable = name == USB
                    return true
                }
                code < 0 -> {
                    if (name == USB) RecorderState.usbAvailable = false
                    unreachable = "$name: $msg"
                }
                else -> {
                    // Server erreicht, aber abgelehnt – über den anderen Weg käme dieselbe Antwort
                    RecorderState.lastError = when (code) {
                        401 -> "Server: falsches Passwort"
                        404 -> "Server: Upload-Adresse nicht gefunden – AleCast aktualisieren?"
                        else -> "Server antwortet $code $msg".trim()
                    }
                    return false
                }
            }
        }
        RecorderState.lastError = if (prefs.transferMode == Prefs.TRANSFER_USB) {
            "USB: AleCast nicht erreichbar – Kabel und USB-Debugging prüfen (Segmente warten im Puffer)"
        } else {
            "Upload nicht möglich – $unreachable"
        }
        return false
    }

    /** Liefert (Antwortcode, Meldung); Code -1 = nicht erreichbar. */
    private fun send(f: File, base: String, prefs: Prefs, start: Long, dur: Long, connectTimeout: Int): Pair<Int, String> {
        val cam = URLEncoder.encode(prefs.camera, "UTF-8")
        val conn = try {
            URL("$base/api/upload?camera=$cam&start=$start&duration=$dur").openConnection() as HttpURLConnection
        } catch (e: Exception) {
            return -1 to (e.message ?: "ungültige Adresse")
        }
        try {
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = connectTimeout
            conn.readTimeout = 120_000
            conn.setFixedLengthStreamingMode(f.length())
            conn.setRequestProperty("Content-Type", "video/mp4")
            authorize(conn, prefs)
            try {
                f.inputStream().use { input -> conn.outputStream.use { input.copyTo(it, 256 * 1024) } }
            } catch (e: java.io.IOException) {
                return -1 to (e.message ?: e.javaClass.simpleName)
            }
            val code = conn.responseCode
            if (code in 200..299) return code to "ok"
            val body = try {
                conn.errorStream?.bufferedReader()?.readText()?.take(200) ?: ""
            } catch (_: Exception) { "" }
            return code to body
        } catch (e: java.io.IOException) {
            return -1 to (e.message ?: e.javaClass.simpleName)
        } finally {
            conn.disconnect()
        }
    }

    /** Hält den lokalen Puffer unter dem eingestellten Limit (älteste Segmente zuerst weg). */
    private fun enforceLimit() {
        val limit = Prefs(ctx).maxLocalMb * 1024L * 1024L
        val files = pending()
        var total = files.sumOf { it.length() }
        for (f in files) {
            if (total <= limit) break
            total -= f.length()
            f.delete()
            RecorderState.lastError = "Lokaler Speicher voll – ältestes Segment verworfen"
        }
    }

    companion object {
        @Volatile private var instance: Uploader? = null

        /** Ein einziger Uploader pro Prozess, damit nie zwei Threads dieselbe Datei senden. */
        @Synchronized
        fun ensure(ctx: Context): Uploader {
            instance?.let { return it }
            val up = Uploader(ctx, RecorderService.segmentDirs(ctx).second)
            instance = up
            return up
        }

        const val USB = "USB"
        const val NET = "Netzwerk"

        /** Port, den AleCast per "adb reverse" vom Handy zum Container durchreicht. */
        const val USB_PORT = 18086

        fun routes(prefs: Prefs): List<Pair<String, String>> = when (prefs.transferMode) {
            Prefs.TRANSFER_USB -> listOf(USB to "http://127.0.0.1:$USB_PORT")
            Prefs.TRANSFER_NET -> listOf(NET to prefs.server)
            else -> listOf(USB to "http://127.0.0.1:$USB_PORT", NET to prefs.server)
        }

        private fun authorize(conn: HttpURLConnection, prefs: Prefs) {
            if (prefs.password.isNotEmpty()) {
                val token = Base64.encodeToString("alecorder:${prefs.password}".toByteArray(), Base64.NO_WRAP)
                conn.setRequestProperty("Authorization", "Basic $token")
            }
        }

        /** Verbindung über alle eingestellten Wege prüfen (blockierend). */
        fun testConnection(ctx: Context): String {
            val prefs = Prefs(ctx)
            return routes(prefs).joinToString("\n") { (name, base) ->
                val r = try {
                    val conn = URL("$base/api/recordings").openConnection() as HttpURLConnection
                    conn.connectTimeout = if (name == USB) 2_000 else 5_000
                    conn.readTimeout = 5_000
                    authorize(conn, prefs)
                    val code = conn.responseCode
                    conn.disconnect()
                    when (code) {
                        200 -> "✓ verbunden"
                        401 -> "✗ Passwort falsch"
                        404 -> "✗ erreichbar, aber AleCast zu alt (Update nötig)"
                        else -> "✗ Antwort $code"
                    }
                } catch (e: Exception) {
                    if (name == USB) "✗ nicht verbunden (Kabel an AleCast? USB-Debugging an?)" else "✗ nicht erreichbar: ${e.message}"
                }
                if (name == USB) RecorderState.usbAvailable = r.startsWith("✓")
                "$name: $r"
            }
        }
    }
}
