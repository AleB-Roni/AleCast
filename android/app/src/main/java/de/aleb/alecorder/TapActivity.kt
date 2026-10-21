package de.aleb.alecorder

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.text.Editable
import android.text.TextWatcher
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

/** Tipp-Punkte setzen: Punkte auf dem Bildschirmfoto platzieren, je Punkt Intervall und Versatz. */
class TapActivity : Activity() {
    private lateinit var prefs: Prefs
    private lateinit var canvas: TapCanvas
    private lateinit var list: LinearLayout
    private lateinit var serviceInfo: TextView
    private lateinit var master: Switch
    private val points = mutableListOf<TapPoint>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        points.addAll(prefs.tapPoints)

        val root = Ui.column(this, 8)

        serviceInfo = Ui.text(this, "", 13f)
        root.addView(serviceInfo)

        canvas = TapCanvas(this).apply {
            bitmap = RecorderState.lastSnapshot
            onAdd = { x, y ->
                val pts = this@TapActivity.points
                pts += TapPoint(x, y)
                refresh(pts.size - 1)
            }
            onMove = { i, x, y ->
                val pts = this@TapActivity.points
                pts[i] = pts[i].copy(x = x, y = y)
                canvas.points = pts.toList()
                updateRowTitle(i)
            }
            onSelect = { i -> canvas.selected = i }
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                (resources.displayMetrics.heightPixels * 0.32f).toInt(),
            )
        }
        root.addView(canvas)
        root.addView(Ui.text(this, "Auf eine Stelle tippen = neuer Punkt · Punkt ziehen = verschieben", 12f, Ui.MUTED).apply {
            setPadding(0, Ui.dp(context, 4), 0, Ui.dp(context, 4))
        })

        list = Ui.column(this, 0)
        root.addView(ScrollView(this).apply {
            addView(list)
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
        })

        master = Switch(this).apply { text = "Tipp-Automatik aktiv"; isChecked = prefs.tapEnabled }
        root.addView(master)
        root.addView(Ui.buttonRow(this,
            Ui.button(this, "Bedienungshilfe") { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) },
            Ui.button(this, "Speichern", primary = true) { save(); finish() },
        ))

        setContentView(root)
        refresh(-1)
    }

    private fun refresh(select: Int) {
        canvas.points = points.toList()
        canvas.selected = select
        list.removeAllViews()
        if (points.isEmpty()) {
            list.addView(Ui.text(this, "Noch keine Punkte. Tippe oben ins Bild, z. B. auf den Aktualisieren-Knopf.", 13f, Ui.MUTED))
        }
        points.forEachIndexed { i, _ -> list.addView(row(i)) }
    }

    private fun row(i: Int): LinearLayout {
        val p = points[i]
        val card = Ui.card(this, "Punkt ${i + 1}")
        card.tag = i
        val title = Ui.text(this, p.describe(), 13f, Ui.MUTED).apply { tag = "title" }
        card.addView(title)

        fun number(value: Int, onChange: (Int) -> Unit): EditText = Ui.field(this, value.toString(), number = true).apply {
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(s: Editable?) { s?.toString()?.toIntOrNull()?.let(onChange) }
            })
        }

        card.addView(Ui.row(this).apply {
            addView(Ui.text(this@TapActivity, "alle", 14f))
            addView(Ui.weighted(number(p.intervalSec) { v -> points[i] = points[i].copy(intervalSec = v.coerceIn(1, 86_400)) }))
            addView(Ui.text(this@TapActivity, "s  · Versatz", 14f))
            addView(Ui.weighted(number(p.offsetSec) { v -> points[i] = points[i].copy(offsetSec = v.coerceIn(0, 86_400)) }))
            addView(Ui.text(this@TapActivity, "s", 14f))
        })

        val on = Switch(this).apply {
            text = "An"; isChecked = p.enabled
            setOnCheckedChangeListener { _, c -> points[i] = points[i].copy(enabled = c); canvas.points = points.toList() }
        }
        card.addView(Ui.row(this).apply {
            addView(Ui.weighted(on))
            addView(Ui.weighted(Ui.button(this@TapActivity, "Test") { test(i) }))
            addView(Ui.weighted(Ui.button(this@TapActivity, "Löschen") {
                points.removeAt(i)
                refresh(-1)
            }))
        })
        card.setOnClickListener { canvas.selected = i }
        return card
    }

    private fun updateRowTitle(i: Int) {
        val card = (0 until list.childCount).map { list.getChildAt(it) }.firstOrNull { it.tag == i } as? LinearLayout ?: return
        (card.findViewWithTag<TextView>("title"))?.text = points[i].describe()
    }

    private fun test(i: Int) {
        val svc = TapService.instance
        if (svc == null) {
            Toast.makeText(this, "Bedienungshilfe „AleCorder Tipp-Automatik“ ist nicht eingeschaltet", Toast.LENGTH_LONG).show()
            return
        }
        save()
        Toast.makeText(this, "Wechsle zur vorherigen App – Punkt ${i + 1} wird in 2 s angetippt", Toast.LENGTH_LONG).show()
        svc.testLater(points[i], i + 1, 2500)
        moveTaskToBack(true)
    }

    private fun save() {
        prefs.tapPoints = points.toList()
        prefs.tapEnabled = master.isChecked
        TapService.reload()
    }

    override fun onResume() {
        super.onResume()
        RecorderState.appVisible = true
        canvas.bitmap = RecorderState.lastSnapshot
        serviceInfo.text = if (TapService.isEnabled(this)) {
            "✓ Bedienungshilfe aktiv · ${RecorderState.tapStatus}" +
                (if (RecorderState.lastTap.isNotEmpty()) " · zuletzt ${RecorderState.lastTap}" else "")
        } else {
            "⚠ Bedienungshilfe „AleCorder Tipp-Automatik“ ist aus. Per USB schaltet AleCast sie automatisch ein, " +
                "sonst unten auf „Bedienungshilfe“ tippen und einschalten."
        }
    }

    override fun onPause() {
        RecorderState.appVisible = false
        super.onPause()
    }
}
