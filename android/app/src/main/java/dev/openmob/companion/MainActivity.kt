package dev.openmob.companion

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Handler
import android.os.Looper
import android.provider.Settings as AndroidSettings
import android.speech.RecognizerIntent
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

/**
 * The companion app's only screen: setup checklist, connection, "ask your agent", and activity.
 * Built in code with plain android.widget views (no AndroidX). Everything visible is derived
 * from AppState / Settings in [render], which runs on every AppState change while resumed.
 */
class MainActivity : Activity() {
    private companion object {
        const val REQ_SPEECH = 1
        const val REQ_NOTIFICATIONS = 2
        const val REQ_SCREEN = 3
        const val MAX_EVENTS = 30
        /** Pairing must be approved with a strong biometric or the screen lock PIN/pattern/password. */
        const val APPROVERS = BiometricManager.Authenticators.BIOMETRIC_STRONG or
            BiometricManager.Authenticators.DEVICE_CREDENTIAL
    }

    /** One row of the setup checklist. */
    private class Step(
        val row: View,
        val mark: TextView,
        val body: TextView,
        val button: Button,
    )

    private enum class Style { PRIMARY, SECONDARY, DANGER }

    private lateinit var settings: Settings
    private val main = Handler(Looper.getMainLooper())
    private var unsubscribe: (() -> Unit)? = null
    private var notificationsDenied = false
    private val background = Executors.newSingleThreadExecutor() // account/pairing HTTP calls
    private var approval: CancellationSignal? = null

    // Setup
    private lateinit var setupProgress: TextView
    private lateinit var pairStep: Step
    private lateinit var a11yStep: Step
    private lateinit var restrictedHelp: LinearLayout
    private lateinit var restrictedToggle: TextView
    private var restrictedHelpOpen = false
    private var sentToA11ySettings = false
    private lateinit var notifStep: Step
    private lateinit var pairForm: LinearLayout
    private lateinit var serverInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var unpairButton: Button

    // Connection
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var connectButton: Button
    private lateinit var liveViewSwitch: Switch
    private lateinit var connectionWarning: TextView

    // Ask
    private lateinit var askCard: LinearLayout
    private lateinit var askInput: EditText
    private lateinit var sendButton: Button
    private lateinit var micButton: Button

    // Activity
    private lateinit var questionBox: LinearLayout
    private lateinit var questionText: TextView
    private lateinit var eventList: LinearLayout

    private lateinit var deviceIdText: TextView

    // ---------------------------------------------------------------- lifecycle

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        requestWindowFeature(Window.FEATURE_NO_TITLE)
        setContentView(buildContent())
        setUpSystemBars()
        // On recreation (rotation, dark-mode switch) the pairing link was already handled.
        if (savedInstanceState == null) handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        unsubscribe = AppState.listen { render() }
        // Back from Accessibility settings and still off: most likely Android's "Restricted
        // setting" block for apps installed outside the Play Store, so show how to lift it.
        if (sentToA11ySettings && !isAccessibilityOn && restrictedHelpSupported) restrictedHelpOpen = true
        sentToA11ySettings = false
        render() // also re-checks accessibility, e.g. on return from Settings
    }

    override fun onPause() {
        unsubscribe?.invoke()
        unsubscribe = null
        super.onPause()
    }

    override fun onDestroy() {
        approval?.cancel()
        approval = null
        background.shutdown() // lets an in-flight call finish; its UI callback is skipped
        main.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_NOTIFICATIONS && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
            // Denied with "don't ask again" semantics: the next tap goes to the system settings.
            notificationsDenied = !granted &&
                !shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS)
            render()
        }
    }

    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_SCREEN) {
            // Connect either way: declining only means the web dashboard shows screenshots, not video.
            if (resultCode != RESULT_OK) {
                settings.smoothLiveView = false // remembered: Connect won't ask again
                toast(getString(R.string.ui_live_view_declined))
            }
            ConnectionService.start(this, data.takeIf { resultCode == RESULT_OK }, resultCode)
            render()
            return
        }
        if (requestCode != REQ_SPEECH || resultCode != RESULT_OK) return
        val heard = data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
        if (!heard.isNullOrBlank()) sendCommand(heard)
    }

    // ---------------------------------------------------------------- state

    /** Paired by account (device key) or, in legacy token mode, by token. */
    private val isPaired: Boolean
        get() = settings.server.isNotBlank() && (settings.deviceKey.isNotBlank() || settings.token.isNotBlank())

    private val hasCredentials: Boolean
        get() = settings.deviceKey.isNotEmpty() || settings.token.isNotEmpty() || settings.session.isNotEmpty()

    private val isAccessibilityOn: Boolean
        get() = OpenMobAccessibilityService.instance != null

    private val notificationsAllowed: Boolean
        get() = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun isValidServer(url: String): Boolean {
        val trimmed = url.trim()
        if (!trimmed.startsWith("https://") && !trimmed.startsWith("http://")) return false
        return !Uri.parse(trimmed).host.isNullOrBlank()
    }

    private fun hostOf(server: String): String = Uri.parse(server).host ?: server

    // ---------------------------------------------------------------- render

    private fun render() {
        val paired = isPaired
        val a11y = isAccessibilityOn
        val notifs = notificationsAllowed
        val running = ConnectionService.isRunning
        val connected = AppState.connected

        // Setup checklist
        setupProgress.text = getString(R.string.ui_setup_progress, listOf(paired, a11y, notifs).count { it })
        renderStep(pairStep, paired)
        val email = settings.email.takeIf { settings.session.isNotEmpty() }.orEmpty()
        pairStep.body.text = when {
            paired && settings.deviceKey.isNotEmpty() && email.isNotEmpty() ->
                getString(R.string.ui_paired_as, email, hostOf(settings.server))
            paired -> getString(R.string.ui_step_pair_done, hostOf(settings.server))
            email.isNotEmpty() ->
                getString(R.string.ui_step_pair_scan) + "\n" + getString(R.string.ui_signed_in_as, email)
            else -> getString(R.string.ui_step_pair_scan)
        }
        pairStep.button.visibility = View.VISIBLE // "Change" stays available after pairing
        pairStep.button.text = getString(if (paired) R.string.ui_step_pair_change else R.string.ui_step_pair_button)
        // Scanning the dashboard QR code is the main path; manual token entry is the fallback.
        styleButton(pairStep.button, Style.SECONDARY)
        unpairButton.visibility = if (hasCredentials) View.VISIBLE else View.GONE

        renderStep(a11yStep, a11y)
        a11yStep.body.text = getString(if (a11y) R.string.ui_step_a11y_done else R.string.ui_step_a11y_body)
        restrictedToggle.visibility = if (!a11y && restrictedHelpSupported) View.VISIBLE else View.GONE
        restrictedHelp.visibility = if (!a11y && restrictedHelpOpen) View.VISIBLE else View.GONE

        renderStep(notifStep, notifs)
        notifStep.body.text = getString(if (notifs) R.string.ui_step_notif_done else R.string.ui_step_notif_body)

        // Connection
        val dotColor = when {
            connected -> color(R.color.om_green)
            running -> color(R.color.om_amber)
            else -> color(R.color.om_grey)
        }
        (statusDot.background as GradientDrawable).setColor(dotColor)
        statusText.text = AppState.connection
        connectButton.text = getString(if (running) R.string.ui_disconnect else R.string.ui_connect)
        if (liveViewSwitch.isChecked != settings.smoothLiveView) liveViewSwitch.isChecked = settings.smoothLiveView
        styleButton(connectButton, if (running) Style.DANGER else Style.PRIMARY)
        setEnabled(connectButton, running || paired)
        when {
            !paired && !running -> showWarning(getString(R.string.ui_pair_first), R.color.om_text_secondary)
            !a11y -> showWarning(getString(R.string.ui_a11y_warning), R.color.om_question)
            else -> connectionWarning.visibility = View.GONE
        }

        // Ask
        setEnabled(askInput, connected)
        setEnabled(sendButton, connected)
        setEnabled(micButton, connected)
        askInput.hint = getString(if (connected) R.string.ui_ask_hint else R.string.ui_ask_hint_offline)

        // Activity
        val events = AppState.events().take(MAX_EVENTS)
        val latest = events.firstOrNull()
        if (connected && latest?.kind == "question") {
            questionText.text = latest.message
            questionBox.visibility = View.VISIBLE
        } else {
            questionBox.visibility = View.GONE
        }
        renderEvents(events)

        deviceIdText.text = getString(R.string.ui_device_id, settings.deviceId)
    }

    private fun renderStep(step: Step, done: Boolean) {
        step.mark.text = if (done) "✓" else "○"
        step.mark.setTextColor(if (done) color(R.color.om_on_accent) else color(R.color.om_text_tertiary))
        step.mark.background = if (done) {
            GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(color(R.color.om_green)) }
        } else {
            null
        }
        step.button.visibility = if (done) View.GONE else View.VISIBLE
    }

    private fun showWarning(text: String, colorRes: Int) {
        connectionWarning.text = text
        connectionWarning.setTextColor(color(colorRes))
        connectionWarning.visibility = View.VISIBLE
    }

    private fun renderEvents(events: List<AppState.Event>) {
        eventList.removeAllViews()
        if (events.isEmpty()) {
            eventList.addView(text(getString(R.string.ui_activity_empty), 14f, color(R.color.om_text_tertiary)))
            return
        }
        events.forEachIndexed { index, event ->
            val kindColor = kindColor(event.kind)
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.TOP
                if (index > 0) setPadding(0, dp(10), 0, 0)
            }
            val badge = TextView(this).apply {
                text = event.kind.uppercase()
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                letterSpacing = 0.06f
                setTextColor(kindColor)
                background = rounded(withAlpha(kindColor, 0x26), 6)
                setPadding(dp(7), dp(3), dp(7), dp(3))
                minWidth = dp(64)
                gravity = Gravity.CENTER
            }
            val important = event.kind in setOf("question", "result", "error")
            val message = text(
                event.message, 14f,
                if (important) kindColor else color(R.color.om_text),
                bold = event.kind == "question",
            ).apply { setTextIsSelectable(true) }
            row.addView(badge, LinearLayout.LayoutParams(wrap, wrap).apply { topMargin = dp(1) })
            row.addView(message, LinearLayout.LayoutParams(0, wrap, 1f).apply { marginStart = dp(10) })
            eventList.addView(row)
        }
    }

    private fun kindColor(kind: String): Int = when (kind) {
        "question" -> color(R.color.om_question)
        "result" -> color(R.color.om_result)
        "error" -> color(R.color.om_error)
        "heard", "action" -> color(R.color.om_accent)
        else -> color(R.color.om_text_secondary) // status, thought, jev, anything new
    }

    // ---------------------------------------------------------------- actions

    private fun handleIntent(intent: Intent?) {
        // Shortcuts (long-press the app icon, or the Quick Settings tile).
        when (intent?.action) {
            Shortcuts.OPEN_ACCESSIBILITY, Shortcuts.CONNECT, Shortcuts.ASK -> {
                setIntent(Intent(this, MainActivity::class.java))  // handle once, not again on recreate
                runShortcut(intent.action!!)
                return
            }
        }
        val data = intent?.data ?: return
        if (data.scheme != "openmob" || data.host != "connect") return
        val server = data.getQueryParameter("server")?.trim().orEmpty()
        val token = (data.getQueryParameter("t") ?: data.getQueryParameter("token"))?.trim().orEmpty()
        val code = data.getQueryParameter("code")?.trim().orEmpty()
        // Don't re-handle the same link if the activity is recreated from this intent later.
        setIntent(Intent(this, MainActivity::class.java))
        if (code.isNotEmpty()) {
            // Accounts mode: a one-time code that only the owner's account can redeem.
            if (isValidServer(server)) startAccountPairing(server, code) else toast(getString(R.string.ui_bad_link))
            return
        }
        if (!isValidServer(server) || token.isEmpty()) {
            toast(getString(R.string.ui_bad_link))
            return
        }
        pair(server, token)
        if (isAccessibilityOn) reconnect()
    }

    private fun pair(server: String, token: String) {
        useServer(server)
        settings.deviceKey = "" // legacy token mode
        settings.token = token
        toast(getString(R.string.ui_paired_with, hostOf(server)))
        render()
    }

    /** Switch to [server]; credentials for a different server are dropped. */
    private fun useServer(server: String) {
        val normalized = server.trim().trimEnd('/')
        if (normalized == settings.server) return
        settings.session = ""
        settings.email = ""
        settings.deviceKey = ""
        settings.token = ""
        settings.server = normalized
    }

    // ---------------------------------------------------------------- account pairing

    /**
     * QR code with a one-time code: sign in (if needed) → approve with the screen lock → redeem
     * the code for this phone's own device key → connect.
     */
    private fun startAccountPairing(server: String, code: String) {
        if (settings.server != server.trim().trimEnd('/') && ConnectionService.isRunning) {
            ConnectionService.stop(this)
        }
        useServer(server)
        render()
        // Checked first so nobody signs in only to be refused afterwards.
        if (!screenLockReady()) {
            showScreenLockRequired()
            return
        }
        val target = settings.server
        val session = settings.session
        if (session.isEmpty()) {
            showSignIn(code)
            return
        }
        toast(getString(R.string.ui_checking_account, hostOf(target)))
        inBackground({ AccountApi(target).me(session) }) { result ->
            result.onSuccess { me ->
                when {
                    !me.accounts -> showPairError(getString(R.string.ui_no_accounts))
                    me.email == null -> {
                        settings.session = "" // expired or signed out elsewhere
                        settings.email = ""
                        showSignIn(code)
                    }
                    else -> {
                        settings.email = me.email
                        approve(code)
                    }
                }
            }.onFailure { showPairError(it) }
        }
    }

    private fun showSignIn(code: String) {
        val target = settings.server
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(8), dp(24), 0)
        }
        form.addView(text(getString(R.string.ui_signin_explain), 14f, color(R.color.om_text_secondary)))
        form.addView(
            text(getString(R.string.ui_signin_server, hostOf(target)), 14f, color(R.color.om_text), bold = true),
            margins(top = 8),
        )
        val emailInput = input(getString(R.string.ui_email_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            imeOptions = EditorInfo.IME_ACTION_NEXT
            setText(settings.email)
        }
        val passwordInput = input(getString(R.string.ui_password_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            imeOptions = EditorInfo.IME_ACTION_DONE
        }
        val status = text("", 13f, color(R.color.om_error)).apply { visibility = View.GONE }
        form.addView(emailInput, margins(top = 14, width = match))
        form.addView(passwordInput, margins(top = 8, width = match))
        form.addView(status, margins(top = 8))

        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.ui_signin_title)
            .setView(form)
            // Listeners are set after show() so a failed attempt keeps the dialog open.
            .setPositiveButton(R.string.ui_signin, null)
            .setNeutralButton(R.string.ui_create_account, null)
            .setNegativeButton(R.string.ui_cancel) { _, _ -> toast(getString(R.string.ui_pair_cancelled)) }
            .setOnCancelListener { toast(getString(R.string.ui_pair_cancelled)) }
            .create()

        fun submit(create: Boolean) {
            val email = emailInput.text.toString().trim()
            val password = passwordInput.text.toString()
            fun showStatus(message: String, colorRes: Int) {
                status.text = message
                status.setTextColor(color(colorRes))
                status.visibility = View.VISIBLE
            }
            if (email.isEmpty() || password.isEmpty()) {
                showStatus(getString(R.string.ui_signin_missing), R.color.om_error)
                return
            }
            val buttons = listOf(AlertDialog.BUTTON_POSITIVE, AlertDialog.BUTTON_NEUTRAL).map { dialog.getButton(it) }
            buttons.forEach { it.isEnabled = false }
            showStatus(getString(R.string.ui_working), R.color.om_text_secondary)
            inBackground({
                val api = AccountApi(target)
                if (create) api.signup(email, password) else api.login(email, password)
            }) { result ->
                buttons.forEach { it.isEnabled = true }
                result.onSuccess { signedIn ->
                    if (settings.server != target) return@onSuccess // another link was scanned meanwhile
                    settings.session = signedIn.session
                    settings.email = signedIn.email
                    dialog.dismiss()
                    hideKeyboard()
                    render()
                    approve(code)
                }.onFailure { showStatus(it.message ?: it.javaClass.simpleName, R.color.om_error) }
            }
        }

        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener { submit(create = false) }
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener { submit(create = true) }
            passwordInput.setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_DONE) { submit(create = false); true } else false
            }
        }
        dialog.show()
        if (settings.email.isEmpty()) emailInput.requestFocus() else passwordInput.requestFocus()
    }

    private fun screenLockReady(): Boolean =
        getSystemService(BiometricManager::class.java)?.canAuthenticate(APPROVERS) == BiometricManager.BIOMETRIC_SUCCESS

    private fun showScreenLockRequired() {
        AlertDialog.Builder(this)
            .setTitle(R.string.ui_need_screen_lock_title)
            .setMessage(R.string.ui_need_screen_lock)
            .setPositiveButton(R.string.ui_open_security_settings) { _, _ ->
                try {
                    startActivity(Intent(AndroidSettings.ACTION_SECURITY_SETTINGS))
                } catch (e: ActivityNotFoundException) {
                    startActivity(Intent(AndroidSettings.ACTION_SETTINGS))
                }
            }
            .setNegativeButton(R.string.ui_ok, null)
            .show()
    }

    /** The phone's owner approves with fingerprint/face (strong) or the screen lock. */
    private fun approve(code: String) {
        if (!screenLockReady()) {
            showScreenLockRequired()
            return
        }
        val target = settings.server
        val email = settings.email
        approval?.cancel()
        val cancel = CancellationSignal().also { approval = it }
        val prompt = BiometricPrompt.Builder(this)
            .setTitle(getString(R.string.ui_approve_title))
            .setSubtitle(getString(R.string.ui_approve_subtitle, email, hostOf(target)))
            .setDescription(getString(R.string.ui_approve_description))
            .setAllowedAuthenticators(APPROVERS) // no negative button allowed with DEVICE_CREDENTIAL
            .build()
        prompt.authenticate(cancel, mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                if (approval === cancel) approval = null
                redeem(target, email, code)
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                if (approval === cancel) approval = null
                if (isDestroyed) return
                val cancelled = errorCode == BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED ||
                    errorCode == BiometricPrompt.BIOMETRIC_ERROR_CANCELED
                toast(if (cancelled || errString.isNullOrBlank()) getString(R.string.ui_pair_cancelled) else errString.toString())
            }
            // onAuthenticationFailed (one wrong finger) needs nothing: the prompt stays up.
        })
    }

    private fun redeem(target: String, email: String, code: String) {
        val session = settings.session
        inBackground({
            val key = AccountApi(target).redeem(session, code, settings.deviceId, Build.MODEL)
            // Saved here, so a pairing finishes even if the screen is recreated meanwhile.
            if (settings.server == target) {
                settings.deviceKey = key
                settings.token = ""
            }
        }) { result ->
            result.onSuccess {
                toast(getString(R.string.ui_paired_as_toast, hostOf(target), email))
                render()
                if (isAccessibilityOn || ConnectionService.isRunning) reconnect()
            }.onFailure { error ->
                if (error is AccountApi.ApiError && error.status == 401) {
                    // Session no longer valid; the code wasn't used, so sign in and try again.
                    settings.session = ""
                    toast(error.message ?: "")
                    showSignIn(code)
                } else {
                    showPairError(error)
                }
            }
        }
    }

    private fun showPairError(error: Throwable) {
        val detail = error.message ?: error.javaClass.simpleName
        // 400 = the code itself: expired, already used, or made by a different account.
        val codeProblem = error is AccountApi.ApiError && error.status == 400
        showPairError(if (codeProblem) "$detail\n\n${getString(R.string.ui_pair_new_code)}" else detail)
    }

    private fun showPairError(message: String) {
        AlertDialog.Builder(this)
            .setTitle(R.string.ui_pair_failed_title)
            .setMessage(message)
            .setPositiveButton(R.string.ui_ok, null)
            .show()
    }

    private fun onUnpairClicked() {
        AlertDialog.Builder(this)
            .setTitle(R.string.ui_unpair_title)
            .setMessage(R.string.ui_unpair_body)
            .setPositiveButton(R.string.ui_unpair) { _, _ -> unpair() }
            .setNegativeButton(R.string.ui_cancel, null)
            .show()
    }

    private fun unpair() {
        val target = settings.server
        val session = settings.session
        val deviceId = settings.deviceId
        if (ConnectionService.isRunning) ConnectionService.stop(this)
        settings.session = ""
        settings.email = ""
        settings.deviceKey = ""
        settings.token = ""
        // Also revoke the key and end the session on the server, best effort.
        if (session.isNotEmpty() && target.isNotBlank()) background.execute { AccountApi(target).unpair(session, deviceId) }
        toast(getString(R.string.ui_unpaired))
        render()
    }

    /** Runs [work] off the main thread, then [done] on it (skipped if this screen is gone). */
    private fun <T> inBackground(work: () -> T, done: (Result<T>) -> Unit) {
        if (background.isShutdown) return
        background.execute {
            val result = runCatching(work)
            main.post { if (!isDestroyed) done(result) }
        }
    }

    /** Connect with the current settings, restarting the service if it was using older ones. */
    private fun reconnect() {
        if (ConnectionService.isRunning) {
            ConnectionService.stop(this)
            main.postDelayed({ connect() }, 600)
        } else {
            connect()
        }
    }

    /** Start the connection, first asking Android for screen streaming (the "Start recording or
     *  casting?" prompt) so the web dashboard can show smooth video. One tap per connection. */
    @Suppress("DEPRECATION")
    private fun connect() {
        if (!settings.smoothLiveView) {
            ConnectionService.start(this) // no prompt: the dashboard shows screenshots
            return
        }
        askForScreenSharing()
    }

    /** Android's "Start recording or casting?" prompt; the result lands in onActivityResult. */
    @Suppress("DEPRECATION")
    private fun askForScreenSharing() {
        val projections = getSystemService(MediaProjectionManager::class.java)
        val consent = if (Build.VERSION.SDK_INT >= 34) {
            projections.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay()) // whole screen
        } else {
            projections.createScreenCaptureIntent()
        }
        try {
            startActivityForResult(consent, REQ_SCREEN)
        } catch (e: ActivityNotFoundException) {
            ConnectionService.start(this)
        }
    }

    private fun togglePairForm() {
        if (pairForm.visibility == View.VISIBLE) {
            pairForm.visibility = View.GONE
            hideKeyboard()
            return
        }
        serverInput.setText(settings.server)
        tokenInput.setText(settings.token)
        pairForm.visibility = View.VISIBLE
        serverInput.requestFocus()
    }

    private fun savePairForm() {
        val server = serverInput.text.toString().trim()
        val token = tokenInput.text.toString().trim()
        when {
            !isValidServer(server) -> serverInput.error = getString(R.string.ui_invalid_server)
            token.isEmpty() -> tokenInput.error = getString(R.string.ui_missing_token)
            else -> {
                hideKeyboard()
                pairForm.visibility = View.GONE
                val wasRunning = ConnectionService.isRunning
                pair(server, token)
                if (wasRunning) reconnect()
            }
        }
    }

    private fun onAccessibilityClicked() {
        // Prominent disclosure before sending the user to grant the permission.
        AlertDialog.Builder(this)
            .setTitle(R.string.ui_disclosure_title)
            .setMessage(R.string.ui_disclosure_body)
            .setPositiveButton(R.string.ui_disclosure_agree) { _, _ -> openAccessibilitySettings() }
            .setNegativeButton(R.string.ui_disclosure_decline, null)
            .show()
    }

    /** Android 13+ blocks Accessibility for sideloaded apps until "Allow restricted settings". */
    private val restrictedHelpSupported = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU

    private fun buildRestrictedHelp(column: LinearLayout) {
        restrictedToggle = text(getString(R.string.ui_restricted_toggle), 14f, color(R.color.om_accent), bold = true).apply {
            setPadding(0, dp(10), 0, dp(4))
            setOnClickListener {
                restrictedHelpOpen = !restrictedHelpOpen
                render()
            }
        }
        column.addView(restrictedToggle)

        restrictedHelp = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(color(R.color.om_input_bg), 12)
            setPadding(dp(14), dp(12), dp(14), dp(14))
        }
        restrictedHelp.addView(text(getString(R.string.ui_restricted_why), 14f, color(R.color.om_text_secondary)))
        listOf(
            R.string.ui_restricted_step1, R.string.ui_restricted_step2,
            R.string.ui_restricted_step3, R.string.ui_restricted_step4,
        ).forEachIndexed { i, step ->
            restrictedHelp.addView(
                text("${i + 1}.  ${getString(step)}", 14f, color(R.color.om_text)),
                margins(top = if (i == 0) 10 else 6),
            )
        }
        restrictedHelp.addView(button(getString(R.string.ui_restricted_button), Style.SECONDARY) { openAppInfo() }, margins(top = 12))
        column.addView(restrictedHelp, margins(top = 6, width = match))
    }

    private fun openAppInfo() {
        startActivity(
            Intent(AndroidSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))
        )
    }

    private fun openAccessibilitySettings() {
        sentToA11ySettings = true
        try {
            startActivity(Intent(AndroidSettings.ACTION_ACCESSIBILITY_SETTINGS))
        } catch (e: ActivityNotFoundException) {
            startActivity(Intent(AndroidSettings.ACTION_SETTINGS))
        }
    }

    private fun onNotificationsClicked() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (notificationsDenied) {
            startActivity(
                Intent(AndroidSettings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(AndroidSettings.EXTRA_APP_PACKAGE, packageName)
            )
        } else {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFICATIONS)
        }
    }

    private fun runShortcut(action: String) {
        when (action) {
            // Same path as the in-app button: explanation first, then Android's Accessibility settings.
            Shortcuts.OPEN_ACCESSIBILITY -> if (isAccessibilityOn) toast(getString(R.string.ui_step_a11y_done)) else onAccessibilityClicked()
            Shortcuts.CONNECT -> when {
                !isPaired -> toast(getString(R.string.shortcut_need_pairing))
                ConnectionService.isRunning -> toast(AppState.connection)
                else -> connect()
            }
            Shortcuts.ASK -> if (AppState.connected) startVoiceInput() else toast(getString(R.string.shortcut_need_connection))
        }
        render()
    }

    private fun onConnectClicked() {
        if (ConnectionService.isRunning) {
            ConnectionService.stop(this)
        } else if (isPaired) {
            connect()
        }
        render()
    }

    private fun onSendClicked() {
        val text = askInput.text.toString().trim()
        if (text.isEmpty()) return
        if (sendCommand(text)) {
            askInput.text.clear()
            hideKeyboard()
        }
    }

    private fun sendCommand(text: String): Boolean {
        val sent = ConnectionService.sendCommand(text)
        if (!sent) toast(getString(R.string.ui_not_connected))
        return sent
    }

    @Suppress("DEPRECATION")
    private fun startVoiceInput() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.ui_speech_prompt))
            .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        try {
            startActivityForResult(intent, REQ_SPEECH)
        } catch (e: ActivityNotFoundException) {
            toast(getString(R.string.ui_no_speech))
        }
    }

    // ---------------------------------------------------------------- layout

    private fun buildContent(): View {
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(24))
        }

        // 1. Header
        content.addView(text(getString(R.string.app_name), 30f, color(R.color.om_text), bold = true))
        content.addView(
            text(getString(R.string.ui_subtitle), 16f, color(R.color.om_text_secondary)),
            margins(top = 4, bottom = 8),
        )

        content.addView(buildSetupCard())
        content.addView(buildConnectionCard())
        content.addView(buildAskCard())
        content.addView(buildActivityCard())

        // 6. Footer
        content.addView(
            text(getString(R.string.ui_footer), 13f, color(R.color.om_text_secondary)).apply {
                gravity = Gravity.CENTER_HORIZONTAL
            },
            margins(top = 20),
        )
        deviceIdText = text("", 11f, color(R.color.om_text_tertiary)).apply {
            gravity = Gravity.CENTER_HORIZONTAL
            setTextIsSelectable(true)
        }
        content.addView(deviceIdText, margins(top = 6))

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            setBackgroundColor(color(R.color.om_bg))
            addView(content)
        }
        // targetSdk 35 draws edge-to-edge: keep content clear of the bars, cutouts and keyboard.
        scroll.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(
                WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime()
            )
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        return scroll
    }

    // 2. Setup checklist
    private fun buildSetupCard(): View {
        val card = card()
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        header.addView(cardTitle(getString(R.string.ui_setup_title)), LinearLayout.LayoutParams(0, wrap, 1f))
        setupProgress = text("", 13f, color(R.color.om_text_tertiary))
        header.addView(setupProgress)
        card.addView(header)

        pairStep = step(getString(R.string.ui_step_pair_title)) { togglePairForm() }
        unpairButton = button(getString(R.string.ui_unpair), Style.DANGER) { onUnpairClicked() }
        (pairStep.button.parent as LinearLayout).addView(unpairButton, margins(top = 8))
        card.addView(pairStep.row)
        pairForm = buildPairForm()
        card.addView(pairForm)

        a11yStep = step(getString(R.string.ui_step_a11y_title)) { onAccessibilityClicked() }
        a11yStep.button.text = getString(R.string.ui_step_a11y_button)
        buildRestrictedHelp(a11yStep.button.parent as LinearLayout)
        card.addView(a11yStep.row)

        notifStep = step(getString(R.string.ui_step_notif_title)) { onNotificationsClicked() }
        notifStep.button.text = getString(R.string.ui_step_notif_button)
        card.addView(notifStep.row)
        return card
    }

    private fun buildPairForm(): LinearLayout {
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(dp(38), dp(10), 0, 0)
        }
        serverInput = input(getString(R.string.ui_server_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_NEXT
        }
        tokenInput = input(getString(R.string.ui_token_hint)).apply {
            // Visible-password variation: no autocorrect/suggestions learning the token.
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
            imeOptions = EditorInfo.IME_ACTION_DONE
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_DONE) { savePairForm(); true } else false
            }
        }
        form.addView(serverInput, LinearLayout.LayoutParams(match, wrap))
        form.addView(tokenInput, margins(top = 8, width = match))

        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        buttons.addView(button(getString(R.string.ui_save), Style.PRIMARY) { savePairForm() })
        buttons.addView(
            button(getString(R.string.ui_cancel), Style.SECONDARY) { togglePairForm() },
            margins(start = 8),
        )
        form.addView(buttons, margins(top = 10))
        return form
    }

    // 3. Connection
    private fun buildConnectionCard(): View {
        val card = card()
        card.addView(cardTitle(getString(R.string.ui_connection_title)))

        val statusRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        statusDot = View(this).apply {
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(color(R.color.om_grey)) }
        }
        statusText = text("", 15f, color(R.color.om_text))
        statusRow.addView(statusDot, LinearLayout.LayoutParams(dp(10), dp(10)))
        statusRow.addView(statusText, LinearLayout.LayoutParams(0, wrap, 1f).apply { marginStart = dp(10) })
        card.addView(statusRow, margins(top = 12))

        connectionWarning = text("", 13f, color(R.color.om_question)).apply { visibility = View.GONE }
        card.addView(connectionWarning, margins(top = 8))

        connectButton = button("", Style.PRIMARY) { onConnectClicked() }.apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
            minHeight = dp(54)
        }
        card.addView(connectButton, margins(top = 14, width = match))

        liveViewSwitch = Switch(this).apply {
            text = getString(R.string.ui_live_view)
            textSize = 15f
            setTextColor(color(R.color.om_text))
            isChecked = settings.smoothLiveView
            setOnCheckedChangeListener { _, on ->
                if (on == settings.smoothLiveView) return@setOnCheckedChangeListener
                settings.smoothLiveView = on
                // Turned on while connected: ask now rather than at the next Connect.
                if (on && ConnectionService.isRunning) askForScreenSharing()
            }
        }
        card.addView(liveViewSwitch, margins(top = 16, width = match))
        card.addView(text(getString(R.string.ui_live_view_help), 13f, color(R.color.om_grey)), margins(top = 2))
        return card
    }

    // 4. Ask your agent
    private fun buildAskCard(): View {
        askCard = card()
        askCard.addView(cardTitle(getString(R.string.ui_ask_title)))

        askInput = input(getString(R.string.ui_ask_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            imeOptions = EditorInfo.IME_ACTION_SEND
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_SEND) { onSendClicked(); true } else false
            }
        }
        askCard.addView(askInput, margins(top = 12, width = match))

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        micButton = button(getString(R.string.ui_mic), Style.SECONDARY) { startVoiceInput() }.apply {
            contentDescription = getString(R.string.ui_mic_description)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
        }
        sendButton = button(getString(R.string.ui_send), Style.PRIMARY) { onSendClicked() }
        row.addView(micButton, LinearLayout.LayoutParams(dp(64), dp(48)))
        row.addView(sendButton, LinearLayout.LayoutParams(0, dp(48), 1f).apply { marginStart = dp(10) })
        askCard.addView(row, margins(top = 10, width = match))

        askCard.addView(text(getString(R.string.ui_ask_note), 12f, color(R.color.om_text_tertiary)), margins(top = 10))
        return askCard
    }

    // 5. Activity
    private fun buildActivityCard(): View {
        val card = card()
        card.addView(cardTitle(getString(R.string.ui_activity_title)))

        questionBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            background = rounded(color(R.color.om_warning_bg), 12, stroke = color(R.color.om_question))
            setPadding(dp(14), dp(12), dp(14), dp(14))
        }
        questionBox.addView(text(getString(R.string.ui_question_label), 12f, color(R.color.om_question), bold = true))
        questionText = text("", 17f, color(R.color.om_text), bold = true)
        questionBox.addView(questionText, margins(top = 4))
        val answers = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        answers.addView(
            button(getString(R.string.ui_yes), Style.PRIMARY) { sendCommand("yes") },
            LinearLayout.LayoutParams(0, wrap, 1f),
        )
        answers.addView(
            button(getString(R.string.ui_no), Style.SECONDARY) { sendCommand("no") },
            LinearLayout.LayoutParams(0, wrap, 1f).apply { marginStart = dp(10) },
        )
        questionBox.addView(answers, margins(top = 12, width = match))
        card.addView(questionBox, margins(top = 12, width = match))

        eventList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        card.addView(eventList, margins(top = 12, width = match))
        return card
    }

    // ---------------------------------------------------------------- view helpers

    private val match = ViewGroup.LayoutParams.MATCH_PARENT
    private val wrap = ViewGroup.LayoutParams.WRAP_CONTENT

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + 0.5f).toInt()

    private fun color(id: Int): Int = getColor(id)

    private fun withAlpha(color: Int, alpha: Int): Int =
        Color.argb(alpha, Color.red(color), Color.green(color), Color.blue(color))

    private fun rounded(fill: Int, radiusDp: Int, stroke: Int? = null): GradientDrawable =
        GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(radiusDp).toFloat()
            setColor(fill)
            stroke?.let { setStroke(dp(1), it) }
        }

    private fun margins(
        top: Int = 0,
        bottom: Int = 0,
        start: Int = 0,
        width: Int = wrap,
    ) = LinearLayout.LayoutParams(width, wrap).apply {
        topMargin = dp(top)
        bottomMargin = dp(bottom)
        marginStart = dp(start)
    }

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(color(R.color.om_card), 20, stroke = color(R.color.om_card_stroke))
        setPadding(dp(18), dp(18), dp(18), dp(18))
        layoutParams = margins(top = 16, width = match)
    }

    private fun cardTitle(title: String): TextView = text(title, 18f, color(R.color.om_text), bold = true)

    private fun text(value: String, sizeSp: Float, textColor: Int, bold: Boolean = false): TextView =
        TextView(this).apply {
            text = value
            setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
            setTextColor(textColor)
            if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            setLineSpacing(0f, 1.15f)
        }

    private fun button(label: String, style: Style, onClick: () -> Unit): Button = Button(this).apply {
        text = label
        isAllCaps = false
        stateListAnimator = null
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        minHeight = dp(44)
        minimumHeight = dp(44)
        minWidth = 0
        minimumWidth = 0
        setPadding(dp(18), dp(10), dp(18), dp(10))
        styleButton(this, style)
        setOnClickListener { onClick() }
    }

    private fun styleButton(button: Button, style: Style) {
        val (fill, textColor) = when (style) {
            Style.PRIMARY -> color(R.color.om_accent) to color(R.color.om_on_accent)
            Style.SECONDARY -> color(R.color.om_input_bg) to color(R.color.om_accent)
            Style.DANGER -> withAlpha(color(R.color.om_error), 0x22) to color(R.color.om_error)
        }
        button.setTextColor(textColor)
        button.background = ripple(rounded(fill, 12), withAlpha(textColor, 0x33))
    }

    private fun ripple(content: Drawable, rippleColor: Int): Drawable =
        RippleDrawable(ColorStateList.valueOf(rippleColor), content, null)

    private fun setEnabled(view: View, enabled: Boolean) {
        view.isEnabled = enabled
        view.alpha = if (enabled) 1f else 0.45f
    }

    private fun input(hint: String): EditText = EditText(this).apply {
        this.hint = hint
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        setTextColor(color(R.color.om_text))
        setHintTextColor(color(R.color.om_text_tertiary))
        background = rounded(color(R.color.om_input_bg), 12)
        setPadding(dp(14), dp(12), dp(14), dp(12))
        isSingleLine = true
    }

    /** A checklist row: status mark, title, explanation, and an action button shown until done. */
    private fun step(title: String, onAction: () -> Unit): Step {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.TOP
            layoutParams = margins(top = 16, width = match)
        }
        val mark = TextView(this).apply {
            gravity = Gravity.CENTER
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            typeface = Typeface.DEFAULT_BOLD
        }
        val column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        column.addView(text(title, 16f, color(R.color.om_text), bold = true))
        val body = text("", 14f, color(R.color.om_text_secondary))
        column.addView(body, margins(top = 2))
        val button = button("", Style.PRIMARY, onAction)
        column.addView(button, margins(top = 10))

        row.addView(mark, LinearLayout.LayoutParams(dp(26), dp(26)))
        row.addView(column, LinearLayout.LayoutParams(0, wrap, 1f).apply { marginStart = dp(12) })
        return Step(row, mark, body, button)
    }

    private fun hideKeyboard() {
        val view = currentFocus ?: window.decorView
        getSystemService(InputMethodManager::class.java)?.hideSoftInputFromWindow(view.windowToken, 0)
    }

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_SHORT).show()

    @Suppress("DEPRECATION")
    private fun setUpSystemBars() {
        window.setDecorFitsSystemWindows(false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        window.decorView.setBackgroundColor(color(R.color.om_bg))
        val night = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES
        val light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or
            WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
        window.insetsController?.setSystemBarsAppearance(if (night) 0 else light, light)
    }
}
