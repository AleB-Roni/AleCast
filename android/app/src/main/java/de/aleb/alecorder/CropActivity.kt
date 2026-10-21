package de.aleb.alecorder

import android.app.Activity
import android.os.Bundle
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Ausschnitt auf dem letzten Bildschirmfoto aufziehen und speichern. */
class CropActivity : Activity() {
    private lateinit var view: CropView
    private lateinit var info: android.widget.TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = Prefs(this)
        val root = Ui.column(this, 8)

        info = Ui.text(this, "", 13f, Ui.MUTED).apply { setPadding(Ui.dp(context, 8), 0, Ui.dp(context, 8), Ui.dp(context, 6)) }
        root.addView(info)

        view = CropView(this).apply {
            bitmap = RecorderState.lastSnapshot
            onChange = { c -> showInfo(c) }
            crop = prefs.crop
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
        }
        root.addView(view)

        fun preset(t: String, c: Crop) = Ui.button(this, t) { view.crop = c }
        root.addView(Ui.buttonRow(this,
            preset("Links ½", Crop(0f, 0f, .5f, 1f)),
            preset("Mitte ½", Crop(.25f, 0f, .5f, 1f)),
            preset("Rechts ½", Crop(.5f, 0f, .5f, 1f)),
            preset("Ganz", Crop.FULL),
        ))
        root.addView(Ui.buttonRow(this,
            preset("Oben ½", Crop(0f, 0f, 1f, .5f)),
            preset("Zentrum", Crop(.25f, .25f, .5f, .5f)),
            preset("Unten ½", Crop(0f, .5f, 1f, .5f)),
        ))
        root.addView(Ui.buttonRow(this,
            Ui.button(this, "Abbrechen") { finish() },
            Ui.button(this, "Übernehmen", primary = true) {
                prefs.crop = view.crop
                RecorderService.reconfigure(this)
                Toast.makeText(this, "Ausschnitt gespeichert", Toast.LENGTH_SHORT).show()
                finish()
            },
        ))
        setContentView(root)
        showInfo(view.crop)
    }

    private fun showInfo(c: Crop) {
        val snap = RecorderState.lastSnapshotAt
        val bild = when {
            RecorderState.lastSnapshot == null && !RecorderState.running ->
                "Starte zuerst die Aufnahme und öffne die Tapo-App – dann hier den Ausschnitt wählen."
            RecorderState.lastSnapshot == null ->
                "Noch kein Bild: Wechsle kurz zur Tapo-App und komm dann hierher zurück."
            else -> "Bild von " + SimpleDateFormat("HH:mm:ss", Locale.GERMANY).format(Date(snap)) +
                " · Rahmen aufziehen oder verschieben"
        }
        val bereich = if (c.isFull) "ganzer Bildschirm"
        else "X ${(c.x * 100).toInt()} %, Y ${(c.y * 100).toInt()} %, ${(c.w * 100).toInt()} × ${(c.h * 100).toInt()} %"
        info.text = "$bild\nAusschnitt: $bereich"
    }

    override fun onResume() {
        super.onResume()
        RecorderState.appVisible = true
        view.bitmap = RecorderState.lastSnapshot
        showInfo(view.crop)
    }

    override fun onPause() {
        RecorderState.appVisible = false
        super.onPause()
    }
}
