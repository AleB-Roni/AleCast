package de.aleb.alecorder

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.net.http.SslError
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.HttpAuthHandler
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * Verlauf wie bei Frigate: zeigt die Aufnahmen-Seite von AleCast (Zeitstrahl, Bewegung, Wiedergabe)
 * direkt in der App. Nimmt automatisch den USB-Weg, wenn das Handy am Server hängt, sonst das Netzwerk.
 */
class HistoryActivity : Activity() {
    private lateinit var root: FrameLayout
    private lateinit var web: WebView
    private lateinit var message: TextView
    private var fullscreenView: View? = null
    private var fullscreenCallback: WebChromeClient.CustomViewCallback? = null
    private var authTries = 0

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        actionBar?.hide()
        root = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#0F1113")) }
        message = Ui.text(this, "Verbinde mit AleCast …", 15f, Ui.MUTED).apply {
            setPadding(Ui.dp(context, 24), Ui.dp(context, 24), Ui.dp(context, 24), Ui.dp(context, 24))
        }
        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0F1113"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            webViewClient = Client()
            webChromeClient = Chrome()
            visibility = View.INVISIBLE
        }
        root.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(message)
        setContentView(root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val prefs = Prefs(this)
        thread {
            val base = pickBase(prefs)
            runOnUiThread {
                if (base == null) {
                    message.text = "AleCast nicht erreichbar.\n\nNetzwerk: ${prefs.server}\n" +
                        "Von unterwegs nur über VPN (z. B. WireGuard) – oder Handy per USB an den Server stecken."
                } else {
                    web.loadUrl("$base/recordings")
                }
            }
        }
    }

    /** USB-Tunnel bevorzugen, wenn er funktioniert; sonst die eingestellte Server-Adresse. */
    private fun pickBase(prefs: Prefs): String? {
        val candidates = mutableListOf<String>()
        if (prefs.transferMode != Prefs.TRANSFER_NET) candidates += "http://127.0.0.1:${Uploader.USB_PORT}"
        candidates += prefs.server
        for (base in candidates) {
            try {
                val c = URL("$base/api/recordings").openConnection() as HttpURLConnection
                c.connectTimeout = if (base.contains("127.0.0.1")) 1500 else 5000
                c.readTimeout = 5000
                val code = c.responseCode
                c.disconnect()
                if (code == 200 || code == 401) return base
            } catch (_: Exception) {
            }
        }
        return null
    }

    private inner class Client : WebViewClient() {
        override fun onPageFinished(view: WebView, url: String) {
            message.visibility = View.GONE
            web.visibility = View.VISIBLE
        }

        override fun onReceivedHttpAuthRequest(view: WebView, handler: HttpAuthHandler, host: String, realm: String) {
            val pw = Prefs(this@HistoryActivity).password
            if (pw.isNotEmpty() && authTries++ < 2) {
                handler.proceed("alecorder", pw)
            } else {
                handler.cancel()
                message.text = "AleCast verlangt ein Passwort – bitte in AleCorder unter „AleCast-Server“ eintragen."
                message.visibility = View.VISIBLE
            }
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) {
                message.text = "Fehler beim Laden: ${error.description}"
                message.visibility = View.VISIBLE
                web.visibility = View.INVISIBLE
            }
        }

        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
            handler.cancel()
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean = false
    }

    /** Vollbild für das Video. */
    private inner class Chrome : WebChromeClient() {
        override fun onShowCustomView(view: View, callback: CustomViewCallback) {
            fullscreenView?.let { onHideCustomView() }
            fullscreenView = view
            fullscreenCallback = callback
            root.addView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            web.visibility = View.GONE
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY)
        }

        override fun onHideCustomView() {
            fullscreenView?.let { root.removeView(it) }
            fullscreenView = null
            fullscreenCallback?.onCustomViewHidden()
            fullscreenCallback = null
            web.visibility = View.VISIBLE
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            fullscreenView != null -> (web.webChromeClient as? WebChromeClient)?.onHideCustomView()
            web.canGoBack() -> web.goBack()
            else -> @Suppress("DEPRECATION") super.onBackPressed()
        }
    }

    override fun onResume() {
        super.onResume()
        RecorderState.appVisible = true
        web.onResume()
    }

    override fun onPause() {
        RecorderState.appVisible = false
        web.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }
}
