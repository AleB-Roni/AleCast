package de.aleb.alecorder

import android.content.Context
import android.content.SharedPreferences

/** Bildausschnitt als Anteil des Bildschirms (0..1), Ursprung oben links. */
data class Crop(val x: Float, val y: Float, val w: Float, val h: Float) {
    val isFull: Boolean get() = x < 0.001f && y < 0.001f && w > 0.999f && h > 0.999f

    fun sanitized(): Crop {
        val nx = x.coerceIn(0f, 0.99f)
        val ny = y.coerceIn(0f, 0.99f)
        val nw = w.coerceIn(0.01f, 1f - nx)
        val nh = h.coerceIn(0.01f, 1f - ny)
        return Crop(nx, ny, nw, nh)
    }

    companion object {
        val FULL = Crop(0f, 0f, 1f, 1f)
    }
}

/** Einstellungen, die der Encoder braucht. */
data class EncoderSettings(
    val crop: Crop,
    val fps: Int,
    val bitrate: Int,
    val maxOut: Int,
    val segmentMs: Long,
)

class Prefs(ctx: Context) {
    private val app = ctx.applicationContext
    private val p: SharedPreferences =
        ctx.applicationContext.getSharedPreferences("alecorder", Context.MODE_PRIVATE)

    var server: String
        get() = p.getString("server", "http://192.168.178.108:8086")!!
        set(v) = p.edit().putString("server", v.trim().trimEnd('/')).apply()

    var password: String
        get() = p.getString("password", "")!!
        set(v) = p.edit().putString("password", v).apply()

    var camera: String
        get() = p.getString("camera", "tapo")!!
        set(v) = p.edit().putString("camera", v.trim()).apply()

    var segmentSec: Int
        get() = p.getInt("segment_sec", 60)
        set(v) = p.edit().putInt("segment_sec", v.coerceIn(10, 600)).apply()

    var fps: Int
        get() = p.getInt("fps", 10)
        set(v) = p.edit().putInt("fps", v.coerceIn(1, 30)).apply()

    var bitrateKbps: Int
        get() = p.getInt("bitrate_kbps", 3000)
        set(v) = p.edit().putInt("bitrate_kbps", v.coerceIn(300, 20000)).apply()

    /** Längere Kante der Ausgabe in Pixeln, 0 = Originalgröße des Ausschnitts. */
    var maxOut: Int
        get() = p.getInt("max_out", 1280)
        set(v) = p.edit().putInt("max_out", v).apply()

    var maxLocalMb: Int
        get() = p.getInt("max_local_mb", 2000)
        set(v) = p.edit().putInt("max_local_mb", v.coerceIn(100, 50000)).apply()

    var keepScreenOn: Boolean
        get() = p.getBoolean("keep_screen_on", true)
        set(v) = p.edit().putBoolean("keep_screen_on", v).apply()

    var autostart: Boolean
        get() = p.getBoolean("autostart", true)
        set(v) = p.edit().putBoolean("autostart", v).apply()

    /** true, solange der Nutzer die Aufnahme laufen lassen will (für Autostart/Watchdog). */
    var shouldRun: Boolean
        get() = p.getBoolean("should_run", false)
        set(v) {
            p.edit().putBoolean("should_run", v).apply()
            // Für den AleCast-Watchdog per adb lesbar
            try {
                app.getExternalFilesDir(null)?.let { java.io.File(it, "should_run").writeText(if (v) "1" else "0") }
            } catch (_: Exception) {}
        }

    var crop: Crop
        get() = Crop(
            p.getFloat("crop_x", 0f), p.getFloat("crop_y", 0f),
            p.getFloat("crop_w", 1f), p.getFloat("crop_h", 1f),
        ).sanitized()
        set(c) {
            val s = c.sanitized()
            p.edit().putFloat("crop_x", s.x).putFloat("crop_y", s.y)
                .putFloat("crop_w", s.w).putFloat("crop_h", s.h).apply()
        }

    fun encoderSettings() = EncoderSettings(
        crop = crop,
        fps = fps,
        bitrate = bitrateKbps * 1000,
        maxOut = maxOut,
        segmentMs = segmentSec * 1000L,
    )
}
