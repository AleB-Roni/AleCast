package de.aleb.alecorder

import android.graphics.Bitmap

/** Gemeinsamer Zustand zwischen Dienst und Oberfläche. */
object RecorderState {
    @Volatile var running = false
    @Volatile var status = "Gestoppt"
    @Volatile var outputSize = ""
    @Volatile var captureSize = ""
    @Volatile var segmentsDone = 0
    @Volatile var queueCount = 0
    @Volatile var queueBytes = 0L
    @Volatile var lastUploadAt = 0L
    @Volatile var uploadedCount = 0
    @Volatile var lastError: String? = null
    @Volatile var lastUploadVia = ""
    @Volatile var usbAvailable = false

    /** Letztes Bild des Bildschirms, aufgenommen während AleCorder NICHT im Vordergrund war. */
    @Volatile var lastSnapshot: Bitmap? = null
    @Volatile var lastSnapshotAt = 0L

    /** Tipp-Automatik */
    @Volatile var tapStatus = "aus"
    @Volatile var lastTap = ""

    /** Wird von den eigenen Activities gesetzt, damit der Snapshot nicht AleCorder selbst zeigt. */
    @Volatile var appVisible = false
}
