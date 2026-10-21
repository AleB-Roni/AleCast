package de.aleb.alecorder

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.view.MotionEvent
import android.view.View
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Zeigt das Bildschirmfoto und lässt einen Ausschnitt aufziehen.
 * Ziehen außerhalb des Rahmens = neuer Rahmen, Ziehen innerhalb = Rahmen verschieben.
 */
class CropView(context: Context) : View(context) {
    var bitmap: Bitmap? = null
        set(v) { field = v; invalidate() }

    var crop: Crop = Crop.FULL
        set(v) { field = v.sanitized(); invalidate(); onChange?.invoke(field) }

    var onChange: ((Crop) -> Unit)? = null

    private val img = RectF()
    private val dim = Paint().apply { color = Color.argb(140, 0, 0, 0) }
    private val border = Paint().apply {
        color = Color.rgb(255, 138, 61); style = Paint.Style.STROKE; strokeWidth = 5f; isAntiAlias = true
    }
    private val label = Paint().apply { color = Color.WHITE; textSize = 34f; isAntiAlias = true }
    private val bmpPaint = Paint(Paint.FILTER_BITMAP_FLAG)

    private var mode = 0 // 0 nichts, 1 neu aufziehen, 2 verschieben
    private var downX = 0f
    private var downY = 0f
    private var startCrop = Crop.FULL

    private fun layoutImage() {
        val b = bitmap ?: return
        val s = min(width / b.width.toFloat(), height / b.height.toFloat())
        val w = b.width * s
        val h = b.height * s
        img.set((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2)
    }

    override fun onDraw(canvas: Canvas) {
        canvas.drawColor(Color.BLACK)
        val b = bitmap
        if (b == null) {
            label.textAlign = Paint.Align.CENTER
            canvas.drawText("Noch kein Bild", width / 2f, height / 2f, label)
            return
        }
        layoutImage()
        canvas.drawBitmap(b, null, img, bmpPaint)
        val r = RectF(
            img.left + crop.x * img.width(), img.top + crop.y * img.height(),
            img.left + (crop.x + crop.w) * img.width(), img.top + (crop.y + crop.h) * img.height(),
        )
        val outside = Path().apply {
            fillType = Path.FillType.EVEN_ODD
            addRect(img, Path.Direction.CW)
            addRect(r, Path.Direction.CW)
        }
        canvas.drawPath(outside, dim)
        canvas.drawRect(r, border)
        label.textAlign = Paint.Align.LEFT
        val txt = "${(crop.w * 100).toInt()} % × ${(crop.h * 100).toInt()} %"
        canvas.drawText(txt, r.left + 10f, max(r.top - 12f, img.top + 36f), label)
    }

    private fun frac(x: Float, y: Float): Pair<Float, Float> =
        ((x - img.left) / img.width()).coerceIn(0f, 1f) to ((y - img.top) / img.height()).coerceIn(0f, 1f)

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        if (bitmap == null) return false
        val (fx, fy) = frac(e.x, e.y)
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = fx; downY = fy; startCrop = crop
                val inside = !crop.isFull && fx > crop.x && fx < crop.x + crop.w && fy > crop.y && fy < crop.y + crop.h
                mode = if (inside) 2 else 1
                parent?.requestDisallowInterceptTouchEvent(true)
            }
            MotionEvent.ACTION_MOVE -> {
                if (mode == 1 && (abs(fx - downX) > 0.01f || abs(fy - downY) > 0.01f)) {
                    crop = Crop(min(downX, fx), min(downY, fy), abs(fx - downX), abs(fy - downY))
                } else if (mode == 2) {
                    val nx = (startCrop.x + fx - downX).coerceIn(0f, 1f - startCrop.w)
                    val ny = (startCrop.y + fy - downY).coerceIn(0f, 1f - startCrop.h)
                    crop = Crop(nx, ny, startCrop.w, startCrop.h)
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> mode = 0
        }
        return true
    }
}
