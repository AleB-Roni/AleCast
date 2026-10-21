package de.aleb.alecorder

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.view.MotionEvent
import android.view.View
import kotlin.math.hypot
import kotlin.math.min

/**
 * Zeigt das Bildschirmfoto mit den Tipp-Punkten.
 * Tippen auf eine freie Stelle = neuer Punkt, Punkt ziehen = verschieben, Punkt antippen = auswählen.
 */
class TapCanvas(context: Context) : View(context) {
    var bitmap: Bitmap? = null
        set(v) { field = v; invalidate() }

    var points: List<TapPoint> = emptyList()
        set(v) { field = v; invalidate() }

    var selected = -1
        set(v) { field = v; invalidate() }

    var onAdd: ((Float, Float) -> Unit)? = null
    var onMove: ((Int, Float, Float) -> Unit)? = null
    var onSelect: ((Int) -> Unit)? = null

    private val img = RectF()
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 5f; color = Color.WHITE }
    private val num = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.BLACK; textAlign = Paint.Align.CENTER; isFakeBoldText = true }
    private val hint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.LTGRAY; textAlign = Paint.Align.CENTER; textSize = 36f }
    private val bmpPaint = Paint(Paint.FILTER_BITMAP_FLAG)
    private val radius get() = resources.displayMetrics.density * 16

    private var dragIndex = -1
    private var downX = 0f
    private var downY = 0f
    private var moved = false

    private fun layoutImage() {
        val b = bitmap
        if (b == null) {
            // Ohne Bild eine Fläche im Bildschirmformat zeigen, damit trotzdem Punkte gesetzt werden können
            val dm = resources.displayMetrics
            val ratio = maxOf(dm.widthPixels, dm.heightPixels).toFloat() / minOf(dm.widthPixels, dm.heightPixels)
            val w = min(width.toFloat(), height * ratio)
            val h = w / ratio
            img.set((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2)
            return
        }
        val s = min(width / b.width.toFloat(), height / b.height.toFloat())
        val w = b.width * s
        val h = b.height * s
        img.set((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2)
    }

    override fun onDraw(canvas: Canvas) {
        canvas.drawColor(Color.BLACK)
        layoutImage()
        val b = bitmap
        if (b != null) {
            canvas.drawBitmap(b, null, img, bmpPaint)
        } else {
            fill.color = 0xFF23272B.toInt()
            canvas.drawRect(img, fill)
            canvas.drawText("Kein Bildschirmfoto – Punkte trotzdem setzbar", img.centerX(), img.centerY(), hint)
        }
        num.textSize = radius
        points.forEachIndexed { i, p ->
            val cx = img.left + p.x * img.width()
            val cy = img.top + p.y * img.height()
            fill.color = when {
                i == selected -> Ui.ACCENT
                p.enabled -> 0xFFFFC08A.toInt()
                else -> Color.GRAY
            }
            canvas.drawCircle(cx, cy, radius, fill)
            canvas.drawCircle(cx, cy, radius, ring)
            canvas.drawText("${i + 1}", cx, cy + radius * 0.35f, num)
        }
    }

    private fun hit(x: Float, y: Float): Int {
        points.forEachIndexed { i, p ->
            val cx = img.left + p.x * img.width()
            val cy = img.top + p.y * img.height()
            if (hypot(x - cx, y - cy) < radius * 1.6f) return i
        }
        return -1
    }

    private fun frac(x: Float, y: Float) =
        ((x - img.left) / img.width()).coerceIn(0f, 1f) to ((y - img.top) / img.height()).coerceIn(0f, 1f)

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = e.x; downY = e.y; moved = false
                dragIndex = hit(e.x, e.y)
                parent?.requestDisallowInterceptTouchEvent(true)
            }
            MotionEvent.ACTION_MOVE -> {
                if (hypot(e.x - downX, e.y - downY) > 12f) moved = true
                if (dragIndex >= 0 && moved) {
                    val (fx, fy) = frac(e.x, e.y)
                    onMove?.invoke(dragIndex, fx, fy)
                }
            }
            MotionEvent.ACTION_UP -> {
                if (!moved) {
                    if (dragIndex >= 0) {
                        onSelect?.invoke(dragIndex)
                    } else if (img.contains(e.x, e.y)) {
                        val (fx, fy) = frac(e.x, e.y)
                        onAdd?.invoke(fx, fy)
                    }
                }
                dragIndex = -1
            }
            MotionEvent.ACTION_CANCEL -> dragIndex = -1
        }
        return true
    }
}
