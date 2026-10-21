package de.aleb.alecorder

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.graphics.Path
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent

/**
 * Bedienungshilfe-Dienst, der die eingestellten Punkte im jeweiligen Intervall antippt
 * (z. B. "Aktualisieren" in der Tapo-App). Tippt nicht, solange AleCorder selbst im Vordergrund ist.
 */
class TapService : AccessibilityService() {

    private val handler = Handler(Looper.getMainLooper())
    private val runners = mutableListOf<Runnable>()

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        reload()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        stopAll()
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        stopAll()
        instance = null
        super.onDestroy()
    }

    private fun stopAll() {
        runners.forEach { handler.removeCallbacks(it) }
        runners.clear()
    }

    /** Punkte neu aus den Einstellungen laden und Zeitpläne neu starten. */
    fun reload() {
        stopAll()
        val prefs = Prefs(this)
        val points = prefs.tapPoints.filter { it.enabled }
        if (!prefs.tapEnabled || points.isEmpty()) {
            RecorderState.tapStatus = if (!prefs.tapEnabled) "aus" else "keine Punkte"
            return
        }
        points.forEachIndexed { i, p ->
            val r = object : Runnable {
                override fun run() {
                    tap(p, i + 1, auto = true)
                    handler.postDelayed(this, p.intervalSec * 1000L)
                }
            }
            runners += r
            handler.postDelayed(r, (p.intervalSec + p.offsetSec) * 1000L)
        }
        RecorderState.tapStatus = "aktiv, ${points.size} Punkt(e)"
    }

    /** Für den Test-Knopf: nach kurzer Pause tippen (Zeit, um zur Ziel-App zu wechseln). */
    fun testLater(p: TapPoint, number: Int, delayMs: Long) {
        handler.postDelayed({ tap(p, number, auto = false) }, delayMs)
    }

    private fun tap(p: TapPoint, number: Int, auto: Boolean) {
        if (auto && RecorderState.appVisible) return // nicht in AleCorder selbst tippen
        val (w, h) = screenSize()
        val x = (p.x * w).coerceIn(1f, w - 1f)
        val y = (p.y * h).coerceIn(1f, h - 1f)
        val path = Path().apply { moveTo(x, y) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 60))
            .build()
        val ok = dispatchGesture(gesture, null, null)
        RecorderState.lastTap = "Punkt $number um " +
            java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.GERMANY).format(java.util.Date()) +
            if (ok) "" else " (fehlgeschlagen)"
    }

    private fun screenSize(): Pair<Float, Float> {
        val dm = DisplayMetrics()
        @Suppress("DEPRECATION")
        (getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(dm)
        return dm.widthPixels.toFloat() to dm.heightPixels.toFloat()
    }

    companion object {
        @Volatile var instance: TapService? = null

        fun reload() {
            instance?.let { svc -> svc.handler.post { svc.reload() } }
        }

        /** Ist der Dienst in den Bedienungshilfen eingeschaltet? */
        fun isEnabled(ctx: Context): Boolean {
            if (instance != null) return true
            val list = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: ""
            return list.contains("${ctx.packageName}/") && list.contains("TapService")
        }
    }
}
