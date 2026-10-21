package de.aleb.alecorder

import android.graphics.Bitmap
import android.graphics.Matrix
import android.graphics.SurfaceTexture
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.EGLContext
import android.opengl.EGLDisplay
import android.opengl.EGLExt
import android.opengl.EGLSurface
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Nimmt die Frames des VirtualDisplay per SurfaceTexture entgegen, schneidet den gewählten
 * Ausschnitt per OpenGL aus, skaliert ihn und rendert mit fester Bildrate in den Encoder.
 * Läuft komplett auf einem eigenen GL-Thread.
 */
class CaptureEngine(
    private val workDir: File,
    private val outDir: File,
    private val onSegment: (File) -> Unit,
    private val onError: (String) -> Unit,
) {
    private val thread = HandlerThread("AleCorder-GL").apply { start() }
    private val handler = Handler(thread.looper)

    private var eglDisplay: EGLDisplay = EGL14.EGL_NO_DISPLAY
    private var eglContext: EGLContext = EGL14.EGL_NO_CONTEXT
    private var eglConfig: EGLConfig? = null
    private var pbuffer: EGLSurface = EGL14.EGL_NO_SURFACE
    private var encSurface: EGLSurface = EGL14.EGL_NO_SURFACE

    private var texId = 0
    private var surfaceTexture: SurfaceTexture? = null
    private var inputSurface: Surface? = null
    private val stMatrix = FloatArray(16)
    private var hasFrame = false

    private var program = 0
    private var aPos = 0
    private var aTex = 0
    private var uTexMatrix = 0
    private var uCrop = 0
    private var uRot = 0
    private var paused = false
    private var encBaseW = 0
    private var encBaseH = 0
    private val quadPos: FloatBuffer = floatBuffer(floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f))
    private val quadTex: FloatBuffer = floatBuffer(floatArrayOf(0f, 0f, 1f, 0f, 0f, 1f, 1f, 1f))

    private var capW = 0
    private var capH = 0
    private var settings: EncoderSettings? = null
    private var encoder: SegmentEncoder? = null
    private var ticking = false
    private var nextTick = 0L
    private var lastSnapshot = 0L

    // ---------- öffentliche API (thread-sicher) ----------

    /** Initialisiert EGL und liefert die Surface, in die das VirtualDisplay zeichnen soll. */
    fun start(width: Int, height: Int): Surface = runSync {
        capW = width
        capH = height
        setupEgl()
        setupGl()
        val st = SurfaceTexture(texId)
        st.setDefaultBufferSize(width, height)
        st.setOnFrameAvailableListener({ onFrame() }, handler)
        surfaceTexture = st
        val s = Surface(st)
        inputSurface = s
        RecorderState.captureSize = "${width}×$height"
        s
    }

    /** Encoder (neu) aufsetzen, z. B. nach geändertem Ausschnitt oder Drehung. */
    fun configure(s: EncoderSettings) = handler.post {
        settings = s
        rebuildEncoder()
    }

    fun resizeCapture(width: Int, height: Int) = handler.post {
        if (width == capW && height == capH) return@post
        capW = width
        capH = height
        surfaceTexture?.setDefaultBufferSize(width, height)
        hasFrame = false
        RecorderState.captureSize = "${width}×$height"
        val s = settings ?: return@post
        val (bw, bh) = baseDims(s)
        if (encoder != null && bw == encBaseW && bh == encBaseH) {
            updatePause(s)  // gleiche Zielgröße: nur pausieren/fortsetzen, kein neuer Encoder
        } else {
            rebuildEncoder()
        }
    }

    /** Bildgröße nach "Bild drehen" (so, wie es aufgenommen werden soll). */
    private fun orientedDims(s: EncoderSettings): Pair<Int, Int> =
        if (s.rotation % 180 == 0) capW to capH else capH to capW

    /** Passt die aktuelle Ausrichtung zur eingestellten Aufnahme-Ausrichtung? */
    private fun matchesLock(s: EncoderSettings): Boolean {
        val (w, h) = orientedDims(s)
        return when (s.orientation) {
            Prefs.ORIENT_LANDSCAPE -> w >= h
            Prefs.ORIENT_PORTRAIT -> h > w
            else -> true
        }
    }

    /** Bezugsgröße für Ausschnitt und Encoder – bei fester Ausrichtung immer in dieser Ausrichtung. */
    private fun baseDims(s: EncoderSettings): Pair<Int, Int> {
        val (w, h) = orientedDims(s)
        return when (s.orientation) {
            Prefs.ORIENT_LANDSCAPE -> max(w, h) to minOf(w, h)
            Prefs.ORIENT_PORTRAIT -> minOf(w, h) to max(w, h)
            else -> w to h
        }
    }

    private fun updatePause(s: EncoderSettings) {
        paused = !matchesLock(s)
        RecorderState.status = if (paused) {
            if (s.orientation == Prefs.ORIENT_LANDSCAPE) "Pausiert – Handy steht hochkant (Aufnahme nur im Querformat)"
            else "Pausiert – Handy liegt quer (Aufnahme nur im Hochformat)"
        } else "Nimmt auf"
    }

    fun release() {
        runSync {
            ticking = false
            handler.removeCallbacksAndMessages(null)
            releaseEncoder()
            surfaceTexture?.release()
            inputSurface?.release()
            if (program != 0) GLES20.glDeleteProgram(program)
            if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
                EGL14.eglMakeCurrent(eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT)
                if (pbuffer != EGL14.EGL_NO_SURFACE) EGL14.eglDestroySurface(eglDisplay, pbuffer)
                EGL14.eglDestroyContext(eglDisplay, eglContext)
                EGL14.eglTerminate(eglDisplay)
            }
            eglDisplay = EGL14.EGL_NO_DISPLAY
        }
        thread.quitSafely()
    }

    // ---------- GL-Thread ----------

    private fun onFrame() {
        val st = surfaceTexture ?: return
        if (!EGL14.eglMakeCurrent(eglDisplay, pbuffer, pbuffer, eglContext)) return
        try {
            st.updateTexImage()
            st.getTransformMatrix(stMatrix)
            hasFrame = true
        } catch (e: Exception) {
            Log.w(TAG, "updateTexImage", e)
        }
    }

    private fun rebuildEncoder() {
        val s = settings ?: return
        releaseEncoder()
        val c = s.crop
        val (bw, bh) = baseDims(s)
        encBaseW = bw
        encBaseH = bh
        val cropW = c.w * bw
        val cropH = c.h * bh
        val longest = max(cropW, cropH)
        val scale = if (s.maxOut > 0 && longest > s.maxOut) s.maxOut / longest else 1f
        val w = align16((cropW * scale).roundToInt())
        val h = align16((cropH * scale).roundToInt())
        try {
            val enc = SegmentEncoder(w, h, s.fps, s.bitrate, s.segmentMs, workDir, outDir, onSegment, onError)
            val attrs = intArrayOf(EGL14.EGL_NONE)
            encSurface = EGL14.eglCreateWindowSurface(eglDisplay, eglConfig, enc.inputSurface, attrs, 0)
            if (encSurface == EGL14.EGL_NO_SURFACE) throw IllegalStateException("eglCreateWindowSurface fehlgeschlagen")
            encoder = enc
            RecorderState.outputSize = "${w}×$h"
            updatePause(s)
            startTicking()
        } catch (e: Exception) {
            Log.e(TAG, "encoder", e)
            onError("Encoder ${w}×$h: ${e.message}")
        }
    }

    private fun releaseEncoder() {
        ticking = false
        handler.removeCallbacks(tick)
        if (encSurface != EGL14.EGL_NO_SURFACE) {
            EGL14.eglMakeCurrent(eglDisplay, pbuffer, pbuffer, eglContext)
            EGL14.eglDestroySurface(eglDisplay, encSurface)
            encSurface = EGL14.EGL_NO_SURFACE
        }
        encoder?.stop()
        encoder = null
    }

    private fun startTicking() {
        ticking = true
        nextTick = SystemClock.uptimeMillis()
        handler.removeCallbacks(tick)
        handler.post(tick)
    }

    private val tick = object : Runnable {
        override fun run() {
            if (!ticking) return
            val s = settings ?: return
            renderFrame()
            maybeSnapshot()
            val interval = 1000L / s.fps
            nextTick += interval
            val now = SystemClock.uptimeMillis()
            if (nextTick < now - interval * 5) nextTick = now // nach Hängern nicht nachholen
            handler.postAtTime(this, nextTick)
        }
    }

    private fun renderFrame() {
        val s = settings ?: return
        if (paused || !hasFrame || encSurface == EGL14.EGL_NO_SURFACE) return
        val enc = encoder ?: return
        if (!EGL14.eglMakeCurrent(eglDisplay, encSurface, encSurface, eglContext)) return
        GLES20.glViewport(0, 0, enc.width, enc.height)
        draw(s.crop, s.rotation)
        EGLExt.eglPresentationTimeANDROID(eglDisplay, encSurface, System.nanoTime())
        EGL14.eglSwapBuffers(eglDisplay, encSurface)
    }

    private fun draw(c: Crop, rotation: Int) {
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)
        GLES20.glUseProgram(program)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texId)
        GLES20.glUniformMatrix4fv(uTexMatrix, 1, false, stMatrix, 0)
        // Texturkoordinaten haben den Ursprung unten links, der Ausschnitt oben links.
        GLES20.glUniform4f(uCrop, c.x, 1f - c.y - c.h, c.w, c.h)
        val rad = Math.toRadians(rotation.toDouble())
        GLES20.glUniform2f(uRot, Math.cos(rad).toFloat(), Math.sin(rad).toFloat())
        GLES20.glEnableVertexAttribArray(aPos)
        GLES20.glVertexAttribPointer(aPos, 2, GLES20.GL_FLOAT, false, 0, quadPos)
        GLES20.glEnableVertexAttribArray(aTex)
        GLES20.glVertexAttribPointer(aTex, 2, GLES20.GL_FLOAT, false, 0, quadTex)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        GLES20.glDisableVertexAttribArray(aPos)
        GLES20.glDisableVertexAttribArray(aTex)
    }

    /** Alle 3 s ein kleines Vorschaubild für den Ausschnitt-Editor (nur wenn AleCorder im Hintergrund ist). */
    private fun maybeSnapshot() {
        val now = SystemClock.uptimeMillis()
        if (RecorderState.appVisible || paused || !hasFrame || now - lastSnapshot < 3000) return
        lastSnapshot = now
        try {
            RecorderState.lastSnapshot = snapshot()
            RecorderState.lastSnapshotAt = System.currentTimeMillis()
        } catch (e: Exception) {
            Log.w(TAG, "snapshot", e)
        }
    }

    private fun snapshot(): Bitmap {
        val rot = settings?.rotation ?: 0
        val (ow, oh) = if (rot % 180 == 0) capW to capH else capH to capW
        val w = minOf(960, ow)
        val h = max(1, (oh.toFloat() * w / ow).roundToInt())
        val tex = IntArray(1)
        val fbo = IntArray(1)
        GLES20.glGenTextures(1, tex, 0)
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, tex[0])
        GLES20.glTexImage2D(GLES20.GL_TEXTURE_2D, 0, GLES20.GL_RGBA, w, h, 0, GLES20.GL_RGBA, GLES20.GL_UNSIGNED_BYTE, null)
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glGenFramebuffers(1, fbo, 0)
        GLES20.glBindFramebuffer(GLES20.GL_FRAMEBUFFER, fbo[0])
        GLES20.glFramebufferTexture2D(GLES20.GL_FRAMEBUFFER, GLES20.GL_COLOR_ATTACHMENT0, GLES20.GL_TEXTURE_2D, tex[0], 0)
        GLES20.glViewport(0, 0, w, h)
        draw(Crop.FULL, rot)
        val buf = ByteBuffer.allocateDirect(w * h * 4).order(ByteOrder.nativeOrder())
        GLES20.glReadPixels(0, 0, w, h, GLES20.GL_RGBA, GLES20.GL_UNSIGNED_BYTE, buf)
        GLES20.glBindFramebuffer(GLES20.GL_FRAMEBUFFER, 0)
        GLES20.glDeleteFramebuffers(1, fbo, 0)
        GLES20.glDeleteTextures(1, tex, 0)
        buf.rewind()
        val raw = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        raw.copyPixelsFromBuffer(buf)
        val flip = Matrix().apply { preScale(1f, -1f) }
        val out = Bitmap.createBitmap(raw, 0, 0, w, h, flip, false)
        raw.recycle()
        return out
    }

    // ---------- EGL/GL-Setup ----------

    private fun setupEgl() {
        eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
        val version = IntArray(2)
        check(EGL14.eglInitialize(eglDisplay, version, 0, version, 1)) { "eglInitialize" }
        val attribs = intArrayOf(
            EGL14.EGL_RED_SIZE, 8, EGL14.EGL_GREEN_SIZE, 8, EGL14.EGL_BLUE_SIZE, 8, EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL_RECORDABLE_ANDROID, 1,
            EGL14.EGL_SURFACE_TYPE, EGL14.EGL_WINDOW_BIT or EGL14.EGL_PBUFFER_BIT,
            EGL14.EGL_NONE,
        )
        val configs = arrayOfNulls<EGLConfig>(1)
        val num = IntArray(1)
        check(EGL14.eglChooseConfig(eglDisplay, attribs, 0, configs, 0, 1, num, 0) && num[0] > 0) { "eglChooseConfig" }
        eglConfig = configs[0]
        eglContext = EGL14.eglCreateContext(
            eglDisplay, eglConfig, EGL14.EGL_NO_CONTEXT,
            intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE), 0,
        )
        check(eglContext != EGL14.EGL_NO_CONTEXT) { "eglCreateContext" }
        pbuffer = EGL14.eglCreatePbufferSurface(
            eglDisplay, eglConfig, intArrayOf(EGL14.EGL_WIDTH, 1, EGL14.EGL_HEIGHT, 1, EGL14.EGL_NONE), 0,
        )
        check(EGL14.eglMakeCurrent(eglDisplay, pbuffer, pbuffer, eglContext)) { "eglMakeCurrent" }
    }

    private fun setupGl() {
        val tex = IntArray(1)
        GLES20.glGenTextures(1, tex, 0)
        texId = tex[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texId)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)

        program = GLES20.glCreateProgram()
        GLES20.glAttachShader(program, shader(GLES20.GL_VERTEX_SHADER, VERTEX))
        GLES20.glAttachShader(program, shader(GLES20.GL_FRAGMENT_SHADER, FRAGMENT))
        GLES20.glLinkProgram(program)
        val ok = IntArray(1)
        GLES20.glGetProgramiv(program, GLES20.GL_LINK_STATUS, ok, 0)
        check(ok[0] == GLES20.GL_TRUE) { "Shader-Link: " + GLES20.glGetProgramInfoLog(program) }
        aPos = GLES20.glGetAttribLocation(program, "aPosition")
        aTex = GLES20.glGetAttribLocation(program, "aTexCoord")
        uTexMatrix = GLES20.glGetUniformLocation(program, "uTexMatrix")
        uCrop = GLES20.glGetUniformLocation(program, "uCrop")
        uRot = GLES20.glGetUniformLocation(program, "uRot")
        android.opengl.Matrix.setIdentityM(stMatrix, 0)
    }

    private fun shader(type: Int, src: String): Int {
        val s = GLES20.glCreateShader(type)
        GLES20.glShaderSource(s, src)
        GLES20.glCompileShader(s)
        val ok = IntArray(1)
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0)
        check(ok[0] == GLES20.GL_TRUE) { "Shader: " + GLES20.glGetShaderInfoLog(s) }
        return s
    }

    private fun <T> runSync(block: () -> T): T {
        if (android.os.Looper.myLooper() == thread.looper) return block()
        val latch = CountDownLatch(1)
        var result: Result<T>? = null
        handler.post {
            result = runCatching(block)
            latch.countDown()
        }
        if (!latch.await(10, TimeUnit.SECONDS)) throw IllegalStateException("GL-Thread antwortet nicht")
        return result!!.getOrThrow()
    }

    companion object {
        private const val TAG = "AleCorder"
        private const val EGL_RECORDABLE_ANDROID = 0x3142

        private fun align16(v: Int) = max(64, (v + 8) / 16 * 16)

        private fun floatBuffer(a: FloatArray): FloatBuffer =
            ByteBuffer.allocateDirect(a.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply {
                put(a); position(0)
            }

        private val VERTEX = """
            uniform mat4 uTexMatrix;
            uniform vec4 uCrop;
            uniform vec2 uRot;
            attribute vec4 aPosition;
            attribute vec2 aTexCoord;
            varying vec2 vTex;
            void main() {
                gl_Position = aPosition;
                vec2 c = uCrop.xy + aTexCoord * uCrop.zw;
                vec2 d = c - vec2(0.5);
                c = vec2(d.x * uRot.x - d.y * uRot.y, d.x * uRot.y + d.y * uRot.x) + vec2(0.5);
                vTex = (uTexMatrix * vec4(c, 0.0, 1.0)).xy;
            }
        """.trimIndent()

        private val FRAGMENT = """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            varying vec2 vTex;
            uniform samplerExternalOES sTex;
            void main() {
                gl_FragColor = texture2D(sTex, vTex);
            }
        """.trimIndent()
    }
}
