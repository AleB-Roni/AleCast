package de.aleb.alecorder

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.os.Bundle
import android.util.Log
import android.view.Surface
import java.io.File
import kotlin.concurrent.thread

/**
 * H.264-Encoder mit Surface-Eingang. Schreibt die Ausgabe in MP4-Segmente fester Länge.
 * Ein neues Segment beginnt immer mit einem Keyframe. Fertige Segmente landen als
 * "<StartMillis>_<DauerMillis>.mp4" im Upload-Ordner.
 */
class SegmentEncoder(
    val width: Int,
    val height: Int,
    private val fps: Int,
    bitrate: Int,
    private val segmentMs: Long,
    private val workDir: File,
    private val outDir: File,
    private val onSegment: (File) -> Unit,
    private val onError: (String) -> Unit,
) {
    private val codec: MediaCodec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
    val inputSurface: Surface
    private val drainThread: Thread

    @Volatile private var stopping = false
    private var format: MediaFormat? = null
    private var muxer: MediaMuxer? = null
    private var track = -1
    private var segFile: File? = null
    private var segStartPts = -1L
    private var segWallStart = 0L
    private var lastPts = -1L
    private var samples = 0
    private var syncRequested = false

    init {
        workDir.mkdirs()
        outDir.mkdirs()
        val f = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
        }
        codec.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        inputSurface = codec.createInputSurface()
        codec.start()
        drainThread = thread(name = "AleCorder-Encoder") { drainLoop() }
    }

    private fun drainLoop() {
        val info = MediaCodec.BufferInfo()
        try {
            while (true) {
                val idx = codec.dequeueOutputBuffer(info, 10_000)
                when {
                    idx == MediaCodec.INFO_TRY_AGAIN_LATER -> Unit
                    idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> format = codec.outputFormat
                    idx >= 0 -> {
                        val buf = codec.getOutputBuffer(idx)
                        if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) info.size = 0
                        if (buf != null && info.size > 0 && format != null) writeSample(buf, info)
                        codec.releaseOutputBuffer(idx, false)
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) break
                    }
                }
            }
        } catch (e: Exception) {
            if (!stopping) onError("Encoder: ${e.message}")
        } finally {
            closeSegment()
            try { codec.stop() } catch (_: Exception) {}
            try { codec.release() } catch (_: Exception) {}
            try { inputSurface.release() } catch (_: Exception) {}
        }
    }

    private fun writeSample(buf: java.nio.ByteBuffer, info: MediaCodec.BufferInfo) {
        val pts = info.presentationTimeUs
        val key = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
        if (muxer == null) {
            if (!key) { requestSync(); return }
            openSegment(pts)
        } else if (pts - segStartPts >= segmentMs * 1000) {
            if (key) {
                closeSegment()
                openSegment(pts)
            } else {
                requestSync()
            }
        }
        val m = muxer ?: return
        if (pts <= lastPts) return // Zeitstempel müssen steigen
        val out = MediaCodec.BufferInfo()
        out.set(0, info.size, pts - segStartPts, info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME)
        buf.position(info.offset)
        buf.limit(info.offset + info.size)
        m.writeSampleData(track, buf, out)
        lastPts = pts
        samples++
    }

    private fun requestSync() {
        if (syncRequested) return
        syncRequested = true
        try {
            codec.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        } catch (_: Exception) {}
    }

    private fun openSegment(pts: Long) {
        val fmt = format ?: return
        val nowUs = System.nanoTime() / 1000
        segWallStart = System.currentTimeMillis() - (nowUs - pts) / 1000
        val file = File(workDir, "rec_$segWallStart.mp4")
        val m = MediaMuxer(file.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        track = m.addTrack(fmt)
        m.start()
        muxer = m
        segFile = file
        segStartPts = pts
        lastPts = -1L
        samples = 0
        syncRequested = false
    }

    private fun closeSegment() {
        val m = muxer ?: return
        val file = segFile
        muxer = null
        segFile = null
        var ok = samples > 1
        try { m.stop() } catch (e: Exception) { ok = false; Log.w(TAG, "muxer stop", e) }
        try { m.release() } catch (_: Exception) {}
        if (file == null) return
        if (!ok) { file.delete(); return }
        val durMs = (lastPts - segStartPts) / 1000 + 1000L / fps
        val target = File(outDir, "${segWallStart}_$durMs.mp4")
        if (file.renameTo(target)) {
            RecorderState.segmentsDone++
            onSegment(target)
        } else {
            file.delete()
        }
    }

    /** Beendet den Encoder und schreibt das laufende Segment fertig. */
    fun stop() {
        if (stopping) return
        stopping = true
        try { codec.signalEndOfInputStream() } catch (_: Exception) {}
        drainThread.join(5000)
        if (drainThread.isAlive) {
            drainThread.interrupt()
        }
    }

    companion object {
        private const val TAG = "AleCorder"
    }
}
