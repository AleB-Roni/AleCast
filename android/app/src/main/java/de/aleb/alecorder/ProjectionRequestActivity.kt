package de.aleb.alecorder

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.widget.Toast

/**
 * Unsichtbare Activity, die die Erlaubnis zur Bildschirmaufnahme holt und dann den Dienst startet.
 * Wird von der App, beim Booten und vom AleCast-Watchdog (per adb) aufgerufen.
 * Ist per adb "appops set de.aleb.alecorder PROJECT_MEDIA allow" gesetzt, erscheint kein Dialog.
 */
class ProjectionRequestActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = Prefs(this)
        val fromWatchdog = intent?.getBooleanExtra(EXTRA_AUTO, false) == true
        if (RecorderState.running || (fromWatchdog && !prefs.shouldRun)) {
            finish()
            return
        }
        val mpm = getSystemService(MediaProjectionManager::class.java)
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), REQUEST)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQUEST) {
            if (resultCode == RESULT_OK && data != null) {
                Prefs(this).shouldRun = true
                RecorderService.start(this, resultCode, data)
            } else {
                Toast.makeText(this, "Bildschirmaufnahme nicht erlaubt", Toast.LENGTH_LONG).show()
            }
        }
        finish()
    }

    companion object {
        const val EXTRA_AUTO = "auto"
        private const val REQUEST = 42
    }
}
