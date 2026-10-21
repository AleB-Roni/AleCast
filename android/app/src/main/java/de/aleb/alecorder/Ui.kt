package de.aleb.alecorder

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** Kleine Helfer für die per Code gebaute Oberfläche. */
object Ui {
    const val ACCENT = 0xFFFF8A3D.toInt()
    const val PANEL = 0xFF1A1D20.toInt()
    const val MUTED = 0xFF8B9299.toInt()

    fun dp(ctx: Context, v: Int) = (v * ctx.resources.displayMetrics.density).toInt()

    fun column(ctx: Context, padDp: Int = 16) = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        val p = dp(ctx, padDp)
        setPadding(p, p, p, p)
    }

    fun row(ctx: Context) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    fun card(ctx: Context, title: String): LinearLayout = column(ctx, 14).apply {
        background = GradientDrawable().apply {
            setColor(PANEL); cornerRadius = dp(ctx, 12).toFloat(); setStroke(dp(ctx, 1), 0xFF2A2E33.toInt())
        }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { bottomMargin = dp(ctx, 12) }
        addView(TextView(ctx).apply {
            text = title.uppercase(); setTextColor(MUTED); textSize = 12f; letterSpacing = 0.08f
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, 0, 0, dp(ctx, 8))
        })
    }

    fun label(ctx: Context, t: String) = TextView(ctx).apply {
        text = t; setTextColor(MUTED); textSize = 13f; setPadding(0, dp(ctx, 6), 0, dp(ctx, 2))
    }

    fun field(ctx: Context, value: String, number: Boolean = false, password: Boolean = false) = EditText(ctx).apply {
        setText(value)
        isSingleLine = true
        inputType = when {
            number -> InputType.TYPE_CLASS_NUMBER
            password -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            else -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
    }

    fun button(ctx: Context, t: String, primary: Boolean = false, onClick: (View) -> Unit) = Button(ctx).apply {
        text = t
        setAllCaps(false)
        if (primary) {
            background = GradientDrawable().apply { setColor(ACCENT); cornerRadius = dp(ctx, 8).toFloat() }
            setTextColor(0xFF1A1006.toInt())
            setTypeface(typeface, Typeface.BOLD)
        }
        setOnClickListener(onClick)
    }

    fun weighted(v: View, marginDp: Int = 4): View {
        v.layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
            val m = dp(v.context, marginDp); setMargins(m, m, m, m)
        }
        return v
    }

    fun buttonRow(ctx: Context, vararg buttons: View) = row(ctx).apply {
        buttons.forEach { addView(weighted(it)) }
    }

    fun text(ctx: Context, t: String = "", size: Float = 14f, color: Int = Color.WHITE) = TextView(ctx).apply {
        text = t; textSize = size; setTextColor(color)
    }
}
