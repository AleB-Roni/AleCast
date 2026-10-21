package de.aleb.alecorder

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Startet die Aufnahme nach einem Neustart wieder, wenn Autostart an ist und vorher aufgenommen wurde. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val prefs = Prefs(context)
        Uploader.ensure(context) // liegengebliebene Segmente hochladen
        if (!prefs.autostart || !prefs.shouldRun) return
        try {
            context.startActivity(
                Intent(context, ProjectionRequestActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    .putExtra(ProjectionRequestActivity.EXTRA_AUTO, true),
            )
        } catch (_: Exception) {
            // Ohne "Über anderen Apps einblenden" darf Android die Activity im Hintergrund nicht starten.
        }
    }
}
