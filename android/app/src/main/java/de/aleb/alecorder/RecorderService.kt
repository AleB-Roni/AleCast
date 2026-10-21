package de.aleb.alecorder

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.DisplayMetrics
import android.view.Display
import android.view.WindowManager
import java.io.File

/** Vordergrund-Dienst: hält die Bildschirmaufnahme, den Encoder und den Upload am Laufen. */
class RecorderService : Service() {

    private val main = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var engine: CaptureEngine? = null
    private var uploader: Uploader? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var capW = 0
    private var capH = 0
    private var dpi = 0

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) {}
        override fun onDisplayRemoved(displayId: Int) {}
        override fun onDisplayChanged(displayId: Int) {
            if (displayId != Display.DEFAULT_DISPLAY) return
            val (w, h, d) = displaySize()
            if (w != capW || h != capH) {
                capW = w; capH = h; dpi = d
                virtualDisplay?.resize(w, h, d)
                engine?.resizeCapture(w, h)
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                Prefs(this).shouldRun = false
                shutdown()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RECONFIGURE -> {
                engine?.configure(Prefs(this).encoderSettings())
                return START_STICKY
            }
        }

        startForegroundCompat()
        if (projection != null) return START_STICKY

        val code = intent?.getIntExtra(EXTRA_CODE, 0) ?: 0
        val data: Intent? = if (Build.VERSION.SDK_INT >= 33) {
            intent?.getParcelableExtra(EXTRA_DATA, Intent::class.java)
        } else {
            @Suppress("DEPRECATION") intent?.getParcelableExtra(EXTRA_DATA)
        }
        if (data == null) {
            RecorderState.status = "Keine Aufnahme-Erlaubnis"
            stopSelf()
            return START_NOT_STICKY
        }

        try {
            startCapture(code, data)
        } catch (e: Exception) {
            RecorderState.lastError = "Start fehlgeschlagen: ${e.message}"
            shutdown()
            stopSelf()
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    private fun startCapture(code: Int, data: Intent) {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val mp = mpm.getMediaProjection(code, data) ?: throw IllegalStateException("MediaProjection verweigert")
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                main.post {
                    RecorderState.status = "Aufnahme vom System beendet"
                    shutdown()
                    stopSelf()
                }
            }
        }, main)

        val dirs = segmentDirs(this)
        val up = Uploader.ensure(this)
        uploader = up

        val (w, h, d) = displaySize()
        capW = w; capH = h; dpi = d
        val eng = CaptureEngine(
            workDir = dirs.first,
            outDir = dirs.second,
            onSegment = { up.wake() },
            onError = { msg -> RecorderState.lastError = msg },
        )
        engine = eng
        val surface = eng.start(w, h)
        virtualDisplay = mp.createVirtualDisplay(
            "AleCorder", w, h, d,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            surface, null, main,
        )
        eng.configure(Prefs(this).encoderSettings())
        getSystemService(DisplayManager::class.java).registerDisplayListener(displayListener, main)

        if (Prefs(this).keepScreenOn) acquireScreenLock()
        RecorderState.running = true
        RecorderState.status = "Nimmt auf"
        RecorderState.lastError = null
    }

    @Suppress("DEPRECATION")
    private fun acquireScreenLock() {
        val pm = getSystemService(PowerManager::class.java)
        wakeLock = pm.newWakeLock(
            PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
            "AleCorder:screen",
        ).apply { acquire() }
    }

    private fun displaySize(): Triple<Int, Int, Int> {
        val dm = DisplayMetrics()
        @Suppress("DEPRECATION")
        (getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(dm)
        return Triple(dm.widthPixels, dm.heightPixels, dm.densityDpi)
    }

    private fun shutdown() {
        try { getSystemService(DisplayManager::class.java).unregisterDisplayListener(displayListener) } catch (_: Exception) {}
        val eng = engine
        engine = null
        // Encoder sauber beenden, damit das letzte Segment vollständig geschrieben wird
        eng?.release()
        virtualDisplay?.release()
        virtualDisplay = null
        val mp = projection
        projection = null
        try { mp?.stop() } catch (_: Exception) {}
        // Der Uploader läuft weiter und lädt noch offene Segmente hoch
        uploader?.wake()
        uploader = null
        try { wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
        RecorderState.running = false
        if (RecorderState.status == "Nimmt auf") RecorderState.status = "Gestoppt"
    }

    override fun onDestroy() {
        shutdown()
        super.onDestroy()
    }

    private fun startForegroundCompat() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "Aufnahme", NotificationManager.IMPORTANCE_LOW),
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, RecorderService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n: Notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_rec)
            .setContentTitle("AleCorder nimmt auf")
            .setContentText("Segmente werden an AleCast gesendet")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stopp", stop).build())
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    companion object {
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        const val ACTION_STOP = "de.aleb.alecorder.STOP"
        const val ACTION_RECONFIGURE = "de.aleb.alecorder.RECONFIGURE"
        private const val CHANNEL = "recording"
        private const val NOTIF_ID = 1

        /** (Arbeitsordner für laufende Segmente, Upload-Warteschlange) */
        fun segmentDirs(ctx: Context): Pair<File, File> {
            val base = ctx.getExternalFilesDir(null) ?: ctx.filesDir
            val work = File(base, "recording").apply { mkdirs() }
            val queue = File(base, "queue").apply { mkdirs() }
            return work to queue
        }

        fun start(ctx: Context, code: Int, data: Intent) {
            val i = Intent(ctx, RecorderService::class.java)
                .putExtra(EXTRA_CODE, code)
                .putExtra(EXTRA_DATA, data)
            ctx.startForegroundService(i)
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, RecorderService::class.java).setAction(ACTION_STOP))
        }

        fun reconfigure(ctx: Context) {
            if (!RecorderState.running) return
            ctx.startService(Intent(ctx, RecorderService::class.java).setAction(ACTION_RECONFIGURE))
        }
    }
}
