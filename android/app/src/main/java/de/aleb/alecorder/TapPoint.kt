package de.aleb.alecorder

import org.json.JSONArray
import org.json.JSONObject

/**
 * Ein Punkt, der automatisch angetippt wird.
 * x/y sind Anteile des Bildschirms (0..1, oben links), so passt der Punkt bei jeder Auflösung.
 * Erster Tipp nach [intervalSec] + [offsetSec], danach alle [intervalSec] Sekunden.
 */
data class TapPoint(
    val x: Float,
    val y: Float,
    val intervalSec: Int = 600,
    val offsetSec: Int = 0,
    val enabled: Boolean = true,
) {
    fun describe(): String = "X ${(x * 100).toInt()} % · Y ${(y * 100).toInt()} %"

    companion object {
        fun fromJson(s: String): List<TapPoint> = try {
            val a = JSONArray(s)
            (0 until a.length()).map { i ->
                val o = a.getJSONObject(i)
                TapPoint(
                    x = o.optDouble("x", 0.5).toFloat().coerceIn(0f, 1f),
                    y = o.optDouble("y", 0.5).toFloat().coerceIn(0f, 1f),
                    intervalSec = o.optInt("interval", 600).coerceIn(1, 86_400),
                    offsetSec = o.optInt("offset", 0).coerceIn(0, 86_400),
                    enabled = o.optBoolean("enabled", true),
                )
            }
        } catch (_: Exception) {
            emptyList()
        }

        fun toJson(list: List<TapPoint>): String {
            val a = JSONArray()
            list.forEach {
                a.put(JSONObject().apply {
                    put("x", it.x.toDouble()); put("y", it.y.toDouble())
                    put("interval", it.intervalSec); put("offset", it.offsetSec); put("enabled", it.enabled)
                })
            }
            return a.toString()
        }
    }
}
