package de.aleb.alecorder

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.widget.ArrayAdapter
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private lateinit var status: TextView
    private lateinit var server: android.widget.EditText
    private lateinit var password: android.widget.EditText
    private lateinit var camera: android.widget.EditText
    private lateinit var segment: android.widget.EditText
    private lateinit var fps: android.widget.EditText
    private lateinit var bitrate: android.widget.EditText
    private lateinit var localMb: android.widget.EditText
    private lateinit var maxOut: Spinner
    private lateinit var transfer: Spinner
    private lateinit var keepOn: Switch
    private lateinit var autostart: Switch
    private val ui = Handler(Looper.getMainLooper())
    private val outOptions = listOf(0 to "Original (Ausschnitt)", 640 to "640 px", 960 to "960 px", 1280 to "1280 px", 1920 to "1920 px")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        Uploader.ensure(this)

        val root = Ui.column(this)

        root.addView(Ui.text(this, "AleCorder", 26f, Ui.ACCENT).apply { setTypeface(typeface, android.graphics.Typeface.BOLD) })
        root.addView(Ui.text(this, "Bildschirm aufnehmen und an AleCast senden", 13f, Ui.MUTED).apply {
            setPadding(0, 0, 0, Ui.dp(context, 14))
        })

        // Status + Steuerung
        val ctl = Ui.card(this, "Aufnahme")
        status = Ui.text(this, "", 14f)
        ctl.addView(status)
        ctl.addView(Ui.buttonRow(this,
            Ui.button(this, "▶ Start", primary = true) { save(); startRecording() },
            Ui.button(this, "■ Stopp") { RecorderService.stop(this) },
        ))
        ctl.addView(Ui.buttonRow(this,
            Ui.button(this, "▭ Ausschnitt wählen") { startActivity(Intent(this, CropActivity::class.java)) },
            Ui.button(this, "⊙ Tipp-Punkte") { startActivity(Intent(this, TapActivity::class.java)) },
        ))
        ctl.addView(Ui.buttonRow(this,
            Ui.button(this, "Aufnahmen ansehen") { openRecordings() },
        ))
        root.addView(ctl)

        // Server
        val srv = Ui.card(this, "AleCast-Server")
        srv.addView(Ui.label(this, "Adresse (Web-UI von AleCast)"))
        server = Ui.field(this, prefs.server); srv.addView(server)
        srv.addView(Ui.label(this, "Passwort (leer, wenn keins gesetzt)"))
        password = Ui.field(this, prefs.password, password = true); srv.addView(password)
        srv.addView(Ui.label(this, "Kameraname (A–Z, 0–9, _ und -)"))
        camera = Ui.field(this, prefs.camera); srv.addView(camera)
        srv.addView(Ui.label(this, "Übertragung"))
        transfer = Spinner(this).apply {
            adapter = ArrayAdapter(
                this@MainActivity, android.R.layout.simple_spinner_dropdown_item,
                listOf("Automatisch (USB-Kabel bevorzugt)", "Nur USB-Kabel (kein Netz-Traffic)", "Nur Netzwerk"),
            )
            setSelection(prefs.transferMode)
        }
        srv.addView(transfer)
        srv.addView(Ui.text(this,
            "USB: Handy per Kabel an den Unraid-Server mit AleCast stecken (USB-Debugging an). " +
                "Die Segmente laufen dann durchs Kabel statt übers Netz. Bei „Nur USB“ warten sie im Puffer, bis das Kabel steckt.",
            12f, Ui.MUTED))
        srv.addView(Ui.buttonRow(this, Ui.button(this, "Verbindung testen") { save(); testConnection() }))
        root.addView(srv)

        // Qualität
        val q = Ui.card(this, "Qualität & Segmente")
        q.addView(Ui.label(this, "Segmentlänge (Sekunden)"))
        segment = Ui.field(this, prefs.segmentSec.toString(), number = true); q.addView(segment)
        q.addView(Ui.label(this, "Bilder pro Sekunde"))
        fps = Ui.field(this, prefs.fps.toString(), number = true); q.addView(fps)
        q.addView(Ui.label(this, "Bitrate (kbit/s)"))
        bitrate = Ui.field(this, prefs.bitrateKbps.toString(), number = true); q.addView(bitrate)
        q.addView(Ui.label(this, "Maximale Ausgabegröße (längere Kante)"))
        maxOut = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, outOptions.map { it.second })
            setSelection(outOptions.indexOfFirst { it.first == prefs.maxOut }.coerceAtLeast(0))
        }
        q.addView(maxOut)
        q.addView(Ui.label(this, "Lokaler Puffer bei Server-Ausfall (MB)"))
        localMb = Ui.field(this, prefs.maxLocalMb.toString(), number = true); q.addView(localMb)
        root.addView(q)

        // Dauerbetrieb
        val d = Ui.card(this, "Dauerbetrieb")
        keepOn = Switch(this).apply { text = "Bildschirm während der Aufnahme anlassen"; isChecked = prefs.keepScreenOn }
        autostart = Switch(this).apply { text = "Nach Neustart automatisch weiter aufnehmen"; isChecked = prefs.autostart }
        d.addView(keepOn)
        d.addView(autostart)
        d.addView(Ui.buttonRow(this,
            Ui.button(this, "Über anderen Apps erlauben") {
                startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
            },
            Ui.button(this, "Akku-Optimierung aus") { requestBatteryExemption() },
        ))
        d.addView(Ui.text(this,
            "Tipp: Ist das Handy per USB an AleCast angeschlossen, erlaubt AleCast die Aufnahme automatisch " +
                "ohne Rückfrage und startet AleCorder neu, falls er beendet wurde.", 12f, Ui.MUTED).apply {
            setPadding(0, Ui.dp(context, 8), 0, 0)
        })
        d.addView(Ui.buttonRow(this, Ui.button(this, "Einstellungen speichern", primary = true) {
            save(); RecorderService.reconfigure(this)
            Toast.makeText(this, "Gespeichert", Toast.LENGTH_SHORT).show()
        }))
        root.addView(d)

        setContentView(ScrollView(this).apply { addView(root) })

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun save() {
        prefs.server = server.text.toString()
        prefs.password = password.text.toString()
        prefs.camera = camera.text.toString()
        segment.text.toString().toIntOrNull()?.let { prefs.segmentSec = it }
        fps.text.toString().toIntOrNull()?.let { prefs.fps = it }
        bitrate.text.toString().toIntOrNull()?.let { prefs.bitrateKbps = it }
        localMb.text.toString().toIntOrNull()?.let { prefs.maxLocalMb = it }
        prefs.maxOut = outOptions.getOrNull(maxOut.selectedItemPosition)?.first ?: 1280
        prefs.transferMode = transfer.selectedItemPosition
        prefs.keepScreenOn = keepOn.isChecked
        prefs.autostart = autostart.isChecked
    }

    private fun startRecording() {
        if (RecorderState.running) {
            Toast.makeText(this, "Läuft bereits", Toast.LENGTH_SHORT).show()
            return
        }
        startActivity(Intent(this, ProjectionRequestActivity::class.java))
    }

    private fun testConnection() {
        Toast.makeText(this, "Teste …", Toast.LENGTH_SHORT).show()
        thread {
            val r = Uploader.testConnection(this)
            ui.post {
                android.app.AlertDialog.Builder(this).setTitle("Verbindungstest").setMessage(r)
                    .setPositiveButton("OK", null).show()
            }
        }
    }

    private fun openRecordings() {
        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(prefs.server + "/recordings")))
    }

    private fun requestBatteryExemption() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "Bereits ausgenommen", Toast.LENGTH_SHORT).show()
            return
        }
        @Suppress("BatteryLife")
        startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
    }

    private val refresher = object : Runnable {
        override fun run() {
            status.text = statusText()
            ui.postDelayed(this, 1000)
        }
    }

    private fun statusText(): String {
        val s = RecorderState
        val t = SimpleDateFormat("HH:mm:ss", Locale.GERMANY)
        val sb = StringBuilder()
        sb.append(if (s.running) "● " else "○ ").append(s.status).append('\n')
        if (s.running) sb.append("Bildschirm ${s.captureSize} → Video ${s.outputSize}\n")
        sb.append("Segmente: ${s.segmentsDone} aufgenommen, ${s.uploadedCount} hochgeladen\n")
        sb.append("Warteschlange: ${s.queueCount} (${s.queueBytes / 1_000_000} MB)")
        if (s.lastUploadAt > 0) sb.append(" · letzter Upload ${t.format(Date(s.lastUploadAt))} per ${s.lastUploadVia}")
        sb.append("\nUSB zu AleCast: ").append(if (s.usbAvailable) "verbunden" else "nicht verbunden")
        val crop = prefs.crop
        sb.append("\nAusschnitt: ").append(
            if (crop.isFull) "ganzer Bildschirm"
            else "${(crop.w * 100).toInt()} × ${(crop.h * 100).toInt()} % ab X ${(crop.x * 100).toInt()} %, Y ${(crop.y * 100).toInt()} %",
        )
        val tapOn = TapService.isEnabled(this)
        sb.append("\nTipp-Automatik: ").append(if (tapOn) RecorderState.tapStatus else "Bedienungshilfe aus")
        if (RecorderState.lastTap.isNotEmpty()) sb.append(" · zuletzt ").append(RecorderState.lastTap)
        s.lastError?.let { sb.append("\n⚠ ").append(it) }
        return sb.toString()
    }

    override fun onResume() {
        super.onResume()
        RecorderState.appVisible = true
        ui.post(refresher)
    }

    override fun onPause() {
        RecorderState.appVisible = false
        ui.removeCallbacks(refresher)
        super.onPause()
    }
}
