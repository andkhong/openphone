package dev.openmob.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.AppOpsManager
import android.app.NotificationManager
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Rect
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
import android.os.PowerManager
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
 * The companion app's main screen: pending approvals, setup checklist, connection, "ask your
 * agent", and activity.
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

    // Approvals (also the fallback when notifications are off)
    private lateinit var approvalsBox: LinearLayout

    // Setup
    private lateinit var setupProgress: TextView
    private lateinit var setupSteps: LinearLayout
    private var setupExpanded = false // once everything is done the steps fold away
    private lateinit var pairStep: Step
    private lateinit var a11yStep: Step
    private lateinit var a11yAppInfo: TextView
    private lateinit var restrictedHelp: LinearLayout
    private lateinit var restrictedToggle: TextView
    private var restrictedHelpOpen = false
    private lateinit var notifStep: Step
    private lateinit var batteryStep: Step
    private lateinit var batteryWhy: TextView
    private lateinit var batteryExplain: TextView
    private var batteryExplainOpen = false
    private lateinit var samsungSleepLink: TextView
    private lateinit var screenStep: Step
    private lateinit var tokenToggle: TextView
    private lateinit var pairForm: LinearLayout
    private lateinit var serverInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var unpairButton: Button

    // Connection
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var controllerText: TextView
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
        // An older version kept a full dashboard session after pairing: sign it out and forget it.
        val legacySession = settings.takeLegacySession()
        val legacyServer = settings.server
        if (legacySession.isNotEmpty() && legacyServer.isNotBlank()) {
            background.execute { AccountApi(legacyServer).logout(legacySession) }
        }
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
        get() = settings.deviceKey.isNotEmpty() || settings.token.isNotEmpty()

    private val isAccessibilityOn: Boolean
        get() = OpenMobAccessibilityService.instance != null

    private val notificationPermission: Boolean
        get() = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** The permission, and not switched off afterwards in the app's notification settings. */
    private val notificationsAllowed: Boolean
        get() = notificationPermission && getSystemService(NotificationManager::class.java).areNotificationsEnabled()

    private val batteryUnrestricted: Boolean
        get() = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)

    /** First attempt and acknowledged denial need different next steps. Unknown is not allowed. */
    private val accessibilityRestriction: AccessibilitySetup.Restriction
        get() {
            if (!restrictedHelpSupported) return AccessibilitySetup.Restriction.NONE
            val mode = runCatching {
                getSystemService(AppOpsManager::class.java)
                    .unsafeCheckOpNoThrow("android:access_restricted_settings", applicationInfo.uid, packageName)
            }.getOrNull()
            return AccessibilitySetup.restriction(Build.VERSION.SDK_INT, mode)
        }

    /** https:// anywhere; http:// only on this phone or the local network (see [ServerPolicy]). */
    private fun isValidServer(url: String): Boolean = ServerPolicy.isAllowed(url)

    private fun hostOf(server: String): String = Uri.parse(server).host ?: server

    // ---------------------------------------------------------------- render

    private fun render() {
        val paired = isPaired
        val a11y = isAccessibilityOn
        val notifs = notificationsAllowed
        val battery = batteryUnrestricted
        val running = ConnectionService.isRunning
        val connected = AppState.connected

        renderApprovals(notifs)

        // Setup checklist, in order: pair, Accessibility, notifications, background. Screen sharing
        // joins only once someone has opened the live view (it's never asked for at setup).
        val sharingShown = AppState.liveViewRequested || AppState.screenSharing
        val sharing = AppState.screenSharing
        val steps = listOf(paired, a11y, notifs, battery) + (if (sharingShown) listOf(sharing) else emptyList())
        val allDone = steps.all { it }
        val progress = if (allDone) {
            getString(R.string.ui_setup_all_done)
        } else {
            getString(R.string.ui_setup_progress, steps.count { it }, steps.size)
        }
        setupProgress.text = when {
            !allDone -> progress
            setupExpanded -> "$progress · ${getString(R.string.ui_setup_hide)}"
            else -> "$progress · ${getString(R.string.ui_setup_show)}"
        }
        setupProgress.setTextColor(color(if (allDone) R.color.om_accent else R.color.om_text_tertiary))
        setupSteps.visibility = if (allDone && !setupExpanded) View.GONE else View.VISIBLE
        renderStep(pairStep, paired)
        pairStep.body.text = when {
            paired && settings.deviceKey.isNotEmpty() && settings.email.isNotEmpty() ->
                getString(R.string.ui_paired_as, settings.email, hostOf(settings.server))
            paired -> getString(R.string.ui_step_pair_done, hostOf(settings.server))
            else -> getString(R.string.ui_step_pair_scan)
        }
        // Scanning the dashboard's QR code is the main path; signing in here is the fallback.
        pairStep.button.text = getString(R.string.ui_signin_fallback)
        styleButton(pairStep.button, Style.SECONDARY)
        tokenToggle.visibility = if (paired) View.GONE else View.VISIBLE
        if (paired) pairForm.visibility = View.GONE
        unpairButton.visibility = if (hasCredentials) View.VISIBLE else View.GONE

        renderStep(a11yStep, a11y)
        // Put the required action in the main button; keep troubleshooting collapsed.
        val restriction = accessibilityRestriction
        val restricted = !a11y && restriction == AccessibilitySetup.Restriction.NEEDS_APPROVAL
        a11yStep.body.text = if (a11y) getString(R.string.ui_step_a11y_done) else {
            val next = when {
                restricted -> R.string.ui_step_a11y_restricted
                Build.MANUFACTURER.equals("samsung", ignoreCase = true) -> R.string.ui_step_a11y_next_samsung
                else -> R.string.ui_step_a11y_next
            }
            val firstAttempt = if (restriction == AccessibilitySetup.Restriction.FIRST_ATTEMPT)
                "\n" + getString(R.string.ui_restricted_first_attempt) else ""
            getString(R.string.ui_step_a11y_body) + "\n\n" + getString(next) + firstAttempt
        }
        a11yStep.button.text = getString(if (restricted) R.string.ui_restricted_button else R.string.ui_step_a11y_button)
        a11yAppInfo.visibility = if (!a11y && restrictedHelpSupported && !restricted) View.VISIBLE else View.GONE
        restrictedToggle.visibility = if (!a11y && restrictedHelpSupported) View.VISIBLE else View.GONE
        restrictedHelp.visibility = if (!a11y && restrictedHelpOpen) View.VISIBLE else View.GONE

        renderStep(notifStep, notifs)
        notifStep.body.text = getString(if (notifs) R.string.ui_step_notif_done else R.string.ui_step_notif_body)

        renderStep(batteryStep, battery)
        batteryStep.body.text = getString(if (battery) R.string.ui_step_battery_done else R.string.ui_step_battery_body)
        batteryWhy.visibility = if (battery) View.GONE else View.VISIBLE
        batteryExplain.visibility = if (!battery && batteryExplainOpen) View.VISIBLE else View.GONE
        // Samsung also puts apps to sleep on its own schedule: offer its "Never sleeping apps" list.
        samsungSleepLink.visibility =
            if (battery && Pairing.batteryGuide(Build.MANUFACTURER) == Pairing.BatteryGuide.SAMSUNG) View.VISIBLE else View.GONE

        screenStep.row.visibility = if (sharingShown) View.VISIBLE else View.GONE
        renderStep(screenStep, sharing)
        screenStep.body.text = getString(if (sharing) R.string.ui_step_screen_done else R.string.ui_step_screen_body)

        // The next step to do gets the filled button; the others stay quiet.
        val next = listOf(a11yStep to a11y, notifStep to notifs, batteryStep to battery, screenStep to (sharing || !sharingShown))
            .firstOrNull { paired && !it.second }?.first
        for (step in listOf(a11yStep, notifStep, batteryStep, screenStep)) {
            styleButton(step.button, if (step === next) Style.PRIMARY else Style.SECONDARY)
        }

        // Connection
        val dotColor = when {
            connected -> color(R.color.om_green)
            running -> color(R.color.om_amber)
            else -> color(R.color.om_grey)
        }
        (statusDot.background as GradientDrawable).setColor(dotColor)
        statusText.text = AppState.connection
        val controller = AppState.controller.takeIf { connected }.orEmpty()
        controllerText.text = getString(R.string.controller_using, controller)
        controllerText.visibility = if (controller.isEmpty()) View.GONE else View.VISIBLE
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

    /** Pending approval requests, at the top. The only place they show if notifications are off. */
    private fun renderApprovals(notifications: Boolean) {
        approvalsBox.removeAllViews()
        val requests = Approvals.pending()
        approvalsBox.visibility = if (requests.isEmpty()) View.GONE else View.VISIBLE
        requests.forEachIndexed { index, request ->
            val box = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                background = rounded(color(R.color.om_warning_bg), 16, stroke = color(R.color.om_question))
                setPadding(dp(16), dp(14), dp(16), dp(16))
            }
            box.addView(text(getString(R.string.approval_label), 12f, color(R.color.om_question), bold = true))
            box.addView(text(request.shownQuestion, 17f, color(R.color.om_text), bold = true), margins(top = 4))
            box.addView(
                text(getString(R.string.approval_requester, request.requester), 14f, color(R.color.om_text_secondary)),
                margins(top = 4),
            )
            if (!notifications) {
                box.addView(
                    text(getString(R.string.ui_notifications_off_hint), 12f, color(R.color.om_text_tertiary)),
                    margins(top = 6),
                )
            }
            val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            buttons.addView(
                button(getString(R.string.approval_deny), Style.DANGER) {
                    if (!Approvals.answer(this, request.id, false) && Approvals.get(request.id) != null) {
                        toast(getString(R.string.approval_offline))
                    }
                },
                LinearLayout.LayoutParams(0, wrap, 1f),
            )
            // Approving always goes through the review screen and its screen-lock check.
            buttons.addView(
                button(getString(R.string.ui_approval_review), Style.PRIMARY) {
                    startActivity(Intent(this, ApprovalActivity::class.java).putExtra(Approvals.EXTRA_ID, request.id))
                },
                LinearLayout.LayoutParams(0, wrap, 1f).apply { marginStart = dp(10) },
            )
            box.addView(buttons, margins(top = 12, width = match))
            approvalsBox.addView(box, margins(top = if (index == 0) 0 else 10, width = match))
        }
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
        "question", "approval" -> color(R.color.om_question)
        "controller" -> color(R.color.om_accent)
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
        if (intent?.action == Shortcuts.SHARE_SCREEN) {
            setIntent(Intent(this, MainActivity::class.java))
            if (ConnectionService.isRunning) askForScreenSharing() else toast(getString(R.string.shortcut_need_connection))
            return
        }
        val data = intent?.data ?: return
        // Don't re-handle the same link if the activity is recreated from this intent later.
        setIntent(Intent(this, MainActivity::class.java))
        // A link (from any web page, message, or `openmob phone add` over USB) never changes anything by
        // itself: developer-token links aren't accepted at all, and account links only after the owner
        // confirms the server and account with the screen lock.
        when (val link = Pairing.parse(data.toString())) {
            null -> return
            Pairing.Link.TokenRefused -> toast(getString(R.string.ui_token_link_refused))
            Pairing.Link.Invalid -> toast(getString(R.string.ui_bad_link))
            is Pairing.Link.Account -> startAccountPairing(link.server, link.code)
        }
    }

    /** "Connect to <host>?" for a developer token typed in by hand (account pairing uses the screen-lock prompt). */
    private fun confirmConnect(server: String, onConfirm: () -> Unit) {
        val host = hostOf(server)
        val message = buildString {
            append(getString(R.string.ui_connect_body, host))
            if (isPaired) append("\n\n").append(getString(R.string.ui_connect_replaces, hostOf(settings.server)))
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.ui_connect_title, host))
            .setMessage(message)
            .setPositiveButton(R.string.ui_connect) { _, _ -> onConfirm() }
            .setNegativeButton(R.string.ui_cancel) { _, _ -> toast(getString(R.string.ui_pair_cancelled)) }
            .setOnCancelListener { toast(getString(R.string.ui_pair_cancelled)) }
            .show()
    }

    /** Replacing an existing pairing needs the screen lock (a fresh pairing goes straight on). */
    private fun requireScreenLockToReplace(server: String, onOk: () -> Unit) {
        if (!isPaired) return onOk()
        if (!screenLockReady()) return showScreenLockRequired()
        approval?.cancel()
        val cancel = CancellationSignal().also { approval = it }
        BiometricPrompt.Builder(this)
            .setTitle(getString(R.string.ui_replace_title))
            .setDescription(getString(R.string.ui_approve_replace, hostOf(settings.server), hostOf(server)))
            .setAllowedAuthenticators(APPROVERS)
            .build()
            .authenticate(cancel, mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                    if (approval === cancel) approval = null
                    onOk()
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                    if (approval === cancel) approval = null
                    if (!isDestroyed) toast(getString(R.string.ui_pair_cancelled))
                }
            })
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
        settings.email = ""
        settings.deviceKey = ""
        settings.token = ""
        settings.server = normalized
    }

    // ---------------------------------------------------------------- account pairing

    /** A pairing in progress. Nothing is saved (the current pairing stays as it is) until it succeeds. */
    private class Pending(val target: String, val code: String, val email: String)

    private var pending: Pending? = null

    /**
     * A one-time code from the dashboard's QR code (or `openmob phone add`): ask the server which
     * account it's for, then ONE confirmation, then redeem it for this phone's own device key.
     */
    private fun startAccountPairing(server: String, code: String) {
        // Checked first so nobody gets as far as the prompt only to be refused afterwards.
        if (!screenLockReady()) {
            showScreenLockRequired()
            return
        }
        val target = server.trim().trimEnd('/')
        toast(getString(R.string.ui_checking_code, hostOf(target)))
        inBackground({ AccountApi(target).check(code) }) { result ->
            result.onSuccess { info -> confirmAndPair(Pending(target, code, info.email)) }
                .onFailure { showPairError(it) }
        }
    }

    /** No laptop at hand: sign in here, make a code with that session, and sign straight out again.
     *  The session lives a few seconds in memory; the phone keeps only its device key, as with a QR code. */
    private fun showSignIn() {
        if (!screenLockReady()) {
            showScreenLockRequired()
            return
        }
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(8), dp(24), 0)
        }
        form.addView(text(getString(R.string.ui_signin_explain), 14f, color(R.color.om_text_secondary)))
        val serverInput = input(getString(R.string.ui_server_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_NEXT
            setText(settings.server)
        }
        val emailInput = input(getString(R.string.ui_email_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            imeOptions = EditorInfo.IME_ACTION_NEXT
            setText(settings.email)
        }
        val passwordInput = input(getString(R.string.ui_password_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            imeOptions = EditorInfo.IME_ACTION_DONE
        }
        val inviteInput = input(getString(R.string.ui_invite_hint)).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            imeOptions = EditorInfo.IME_ACTION_DONE
        }
        val status = text("", 13f, color(R.color.om_error)).apply { visibility = View.GONE }
        form.addView(serverInput, margins(top = 14, width = match))
        form.addView(emailInput, margins(top = 8, width = match))
        form.addView(passwordInput, margins(top = 8, width = match))
        form.addView(inviteInput, margins(top = 8, width = match))
        form.addView(status, margins(top = 8))

        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.ui_signin_title)
            .setView(form)
            // Listeners are set after show() so a failed attempt keeps the dialog open.
            .setPositiveButton(R.string.ui_signin, null)
            .setNeutralButton(R.string.ui_create_account, null)
            .setNegativeButton(R.string.ui_cancel, null)
            .create()

        fun submit(create: Boolean) {
            val target = serverInput.text.toString().trim().trimEnd('/')
            val email = emailInput.text.toString().trim()
            val password = passwordInput.text.toString()
            fun showStatus(message: String, colorRes: Int) {
                status.text = message
                status.setTextColor(color(colorRes))
                status.visibility = View.VISIBLE
            }
            if (!isValidServer(target)) return showStatus(getString(R.string.ui_invalid_server), R.color.om_error)
            if (email.isEmpty() || password.isEmpty()) return showStatus(getString(R.string.ui_signin_missing), R.color.om_error)
            val invite = inviteInput.text.toString().trim()
            if (create && invite.isEmpty()) return showStatus(getString(R.string.ui_invite_missing), R.color.om_error)
            val buttons = listOf(AlertDialog.BUTTON_POSITIVE, AlertDialog.BUTTON_NEUTRAL).map { dialog.getButton(it) }
            buttons.forEach { it.isEnabled = false }
            showStatus(getString(R.string.ui_working), R.color.om_text_secondary)
            inBackground({
                val api = AccountApi(target)
                val session = api.signIn(email, password, create, invite)
                val code = try { api.mintCode(session) } finally { api.logout(session) } // never kept
                Pending(target, code, api.check(code).email)
            }) { result ->
                buttons.forEach { it.isEnabled = true }
                result.onSuccess { p ->
                    dialog.dismiss()
                    hideKeyboard()
                    confirmAndPair(p)
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
        (if (settings.server.isEmpty()) serverInput else if (settings.email.isEmpty()) emailInput else passwordInput).requestFocus()
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

    /**
     * The one confirmation: Android's own screen-lock prompt (strong biometric or PIN/pattern/
     * password), titled "Connect this phone to <host>?" with "as <email>" under it. It replaces
     * both the "connect to this server?" dialog and the screen-lock check. When this replaces an
     * existing pairing, the prompt says which one.
     */
    private fun confirmAndPair(p: Pending) {
        if (!screenLockReady()) {
            showScreenLockRequired()
            return
        }
        pending = p
        approval?.cancel()
        val cancel = CancellationSignal().also { approval = it }
        val replacing = isPaired && (settings.server != p.target || settings.email != p.email)
        val description = buildString {
            append(getString(R.string.ui_approve_description))
            if (replacing) append("\n\n").append(getString(R.string.ui_approve_replaces, hostOf(settings.server)))
        }
        val prompt = BiometricPrompt.Builder(this)
            .setTitle(getString(R.string.ui_approve_title_host, hostOf(p.target)))
            .setSubtitle(getString(R.string.ui_approve_as, p.email))
            .setDescription(description)
            .setAllowedAuthenticators(APPROVERS) // no negative button allowed with DEVICE_CREDENTIAL
            .build()
        prompt.authenticate(cancel, mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                if (approval === cancel) approval = null
                if (pending === p) claim(p)
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                if (approval === cancel) approval = null
                if (pending === p) pending = null
                if (isDestroyed) return
                val cancelled = errorCode == BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED ||
                    errorCode == BiometricPrompt.BIOMETRIC_ERROR_CANCELED
                toast(if (cancelled || errString.isNullOrBlank()) getString(R.string.ui_pair_cancelled) else errString.toString())
            }
            // onAuthenticationFailed (one wrong finger) needs nothing: the prompt stays up.
        })
    }

    /** Redeem the code (it's the credential: no session) for this phone's own device key. */
    private fun claim(p: Pending) {
        val wasRunning = ConnectionService.isRunning
        inBackground({
            val claimed = AccountApi(p.target).claim(p.code, settings.deviceId, Build.MODEL, ApprovalKeys.publicKey())
            val oldServer = settings.server
            val oldKey = settings.deviceKey
            // Only now, with the new pairing complete, is the old one replaced. Saved here, so a
            // pairing finishes even if the screen is recreated meanwhile.
            settings.server = p.target
            settings.email = claimed.email.ifBlank { p.email }
            settings.deviceKey = claimed.key
            settings.token = ""
            // Moved to another server: tell the old one, with the old key, so it doesn't linger there.
            if (oldKey.isNotEmpty() && oldServer.isNotBlank() && oldServer != p.target) {
                AccountApi(oldServer).unpair(settings.deviceId, oldKey)
            }
        }) { result ->
            if (pending === p) pending = null
            result.onSuccess {
                toast(getString(R.string.ui_paired_as_toast, hostOf(p.target), settings.email))
                render()
                if (isAccessibilityOn || wasRunning) reconnect()
            }.onFailure { showPairError(it) } // the existing pairing is untouched
        }
    }

    private fun showPairError(error: Throwable) {
        val detail = error.message ?: error.javaClass.simpleName
        // 400 = the code itself: expired, already used, or voided.
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
        val key = settings.deviceKey
        val deviceId = settings.deviceId
        if (ConnectionService.isRunning) ConnectionService.stop(this)
        settings.email = ""
        settings.deviceKey = ""
        settings.token = ""
        // Also revoke the pairing on the server, proven with the device key (best effort).
        if (key.isNotEmpty() && target.isNotBlank()) background.execute { AccountApi(target).unpair(deviceId, key) }
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

    /** Start the connection. No prompt: screen sharing is offered only when someone first opens the
     *  live view (ConnectionService.offerScreenSharing); until then the dashboard shows screenshots. */
    private fun connect() {
        ConnectionService.start(this)
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
                // Developer token pairing (local servers): same confirmation as a link, and the
                // screen lock when it replaces an existing pairing.
                confirmConnect(server) {
                    requireScreenLockToReplace(server) {
                        pairForm.visibility = View.GONE
                        val wasRunning = ConnectionService.isRunning
                        pair(server, token)
                        if (wasRunning || isAccessibilityOn) reconnect()
                    }
                }
            }
        }
    }

    private fun onAccessibilityClicked() {
        // App info only offers approval after Android has shown its initial denial.
        afterDisclosure {
            if (accessibilityRestriction == AccessibilitySetup.Restriction.NEEDS_APPROVAL)
                openAppInfo() else openAccessibilitySettings()
        }
    }

    /** Prominent disclosure and an explicit Agree before any route to the Accessibility switch. */
    private fun afterDisclosure(then: () -> Unit) {
        AlertDialog.Builder(this)
            .setTitle(R.string.ui_disclosure_title)
            .setMessage(R.string.ui_disclosure_body)
            .setPositiveButton(R.string.ui_disclosure_agree) { _, _ -> then() }
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
        restrictedHelp.addView(text(getString(R.string.ui_restricted_steps), 14f, color(R.color.om_text)), margins(top = 8))
        restrictedHelp.addView(text(getString(R.string.ui_restricted_missing), 13f, color(R.color.om_text_tertiary)), margins(top = 8))
        restrictedHelp.addView(button(getString(R.string.ui_restricted_button), Style.SECONDARY) { openAppInfo() }, margins(top = 12))
        restrictedHelp.addView(button(getString(R.string.ui_step_a11y_button), Style.SECONDARY) { afterDisclosure { openAccessibilitySettings() } }, margins(top = 8))
        column.addView(restrictedHelp, margins(top = 6, width = match))
    }

    private fun openAppInfo() {
        startActivity(
            Intent(AndroidSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))
        )
    }

    /**
     * Open Android's supported Accessibility screen and highlight OpenMob where supported.
     * ACCESSIBILITY_DETAILS_SETTINGS requires a system/installer-only permission, so ordinary
     * apps cannot rely on it. Never try to enter a protected fragment by another route.
     * Once enabled, the service returns to OpenMob (AppState.returnAfterAccessibility).
     */
    private fun openAccessibilitySettings() {
        AppState.returnAfterAccessibility = true
        val service = ComponentName(this, OpenMobAccessibilityService::class.java)
        val key = service.flattenToString()
        val attempts = buildList {
            add(Intent(AndroidSettings.ACTION_ACCESSIBILITY_SETTINGS)
                .putExtra(":settings:fragment_args_key", key)
                .putExtra(":settings:show_fragment_args", Bundle().apply { putString(":settings:fragment_args_key", key) }))
            add(Intent(AndroidSettings.ACTION_SETTINGS))
        }
        for (intent in attempts) {
            try {
                startActivity(intent)
                return
            } catch (e: ActivityNotFoundException) {
                // this phone's Settings doesn't have that screen: try the next one
            } catch (e: SecurityException) {
                // some phones keep the per-service screen to themselves
            }
        }
    }

    private fun onNotificationsClicked() {
        // Permission granted (or not needed) but notifications switched off, or denied for good:
        // only the system settings can turn them back on.
        if (notificationPermission || notificationsDenied) {
            startActivity(
                Intent(AndroidSettings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(AndroidSettings.EXTRA_APP_PACKAGE, packageName)
            )
        } else {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFICATIONS)
        }
    }

    /** Straight to Android's own "Let app always run in background?" prompt (one tap: Allow). The
     *  explanation is behind "Why?". On Samsung this sets battery use to Unrestricted; its extra
     *  "Never sleeping apps" list is offered once that's done ([openSamsungSleepingApps]). */
    private fun onBatteryClicked() = requestBatteryExemption()

    /** Samsung's battery settings (Background usage limits › Never sleeping apps), else the app's own page. */
    private fun openSamsungSleepingApps() {
        val attempts = listOf(
            Intent().setComponent(ComponentName("com.samsung.android.lool", "com.samsung.android.sm.battery.ui.BatteryActivity")),
            Intent().setComponent(ComponentName("com.samsung.android.sm", "com.samsung.android.sm.battery.ui.BatteryActivity")),
            Intent(AndroidSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")),
        )
        for (intent in attempts) {
            try {
                startActivity(intent)
                toast(getString(R.string.ui_samsung_sleep_hint))
                return
            } catch (e: ActivityNotFoundException) {
                // not on this phone: try the next one
            } catch (e: SecurityException) {
                // not exported on this One UI version
            }
        }
    }

    @SuppressLint("BatteryLife") // a constant connection is the app's core function, not a convenience
    private fun requestBatteryExemption() {
        try {
            startActivity(
                Intent(AndroidSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            )
        } catch (e: ActivityNotFoundException) {
            startActivity(Intent(AndroidSettings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun runShortcut(action: String) {
        when (action) {
            // Scroll to the setup step; its button shows the disclosure before anything is granted.
            Shortcuts.OPEN_ACCESSIBILITY -> if (isAccessibilityOn) {
                toast(getString(R.string.ui_step_a11y_done))
            } else {
                setupExpanded = true
                a11yStep.row.post {
                    a11yStep.row.requestRectangleOnScreen(Rect(0, 0, a11yStep.row.width, a11yStep.row.height), true)
                }
            }
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

        approvalsBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }
        content.addView(approvalsBox, margins(top = 16, width = match))
        content.addView(buildSetupCard())
        content.addView(buildConnectionCard())
        content.addView(buildIndicatorCard())
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
        setupProgress = text("", 13f, color(R.color.om_text_tertiary)).apply {
            setPadding(dp(8), dp(6), 0, dp(6)) // a bigger target for Show / Hide
            setOnClickListener {
                setupExpanded = !setupExpanded
                render()
            }
        }
        header.addView(setupProgress)
        card.addView(header)

        setupSteps = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        card.addView(setupSteps, margins(width = match))

        pairStep = step(getString(R.string.ui_step_pair_title)) { showSignIn() }
        val pairColumn = pairStep.button.parent as LinearLayout
        tokenToggle = link(getString(R.string.ui_token_toggle)) { togglePairForm() }
        pairColumn.addView(tokenToggle)
        unpairButton = button(getString(R.string.ui_unpair), Style.DANGER) { onUnpairClicked() }
        pairColumn.addView(unpairButton, margins(top = 8))
        setupSteps.addView(pairStep.row)
        pairForm = buildPairForm()
        setupSteps.addView(pairForm)

        a11yStep = step(getString(R.string.ui_step_a11y_title)) { onAccessibilityClicked() }
        a11yStep.button.text = getString(R.string.ui_step_a11y_button)
        a11yAppInfo = link(getString(R.string.ui_restricted_app_info)) { openAppInfo() }
        (a11yStep.button.parent as LinearLayout).addView(a11yAppInfo)
        buildRestrictedHelp(a11yStep.button.parent as LinearLayout)
        setupSteps.addView(a11yStep.row)

        notifStep = step(getString(R.string.ui_step_notif_title)) { onNotificationsClicked() }
        notifStep.button.text = getString(R.string.ui_step_notif_button)
        setupSteps.addView(notifStep.row)

        batteryStep = step(getString(R.string.ui_step_battery_title)) { onBatteryClicked() }
        batteryStep.button.text = getString(R.string.ui_step_battery_button)
        val batteryColumn = batteryStep.button.parent as LinearLayout
        batteryWhy = link(getString(R.string.ui_why)) {
            batteryExplainOpen = !batteryExplainOpen
            render()
        }
        batteryColumn.addView(batteryWhy)
        batteryExplain = text(getString(R.string.ui_battery_body), 13f, color(R.color.om_text_secondary))
        batteryColumn.addView(batteryExplain, margins(top = 2))
        samsungSleepLink = link(getString(R.string.ui_samsung_sleep)) { openSamsungSleepingApps() }
        batteryColumn.addView(samsungSleepLink)
        setupSteps.addView(batteryStep.row)

        screenStep = step(getString(R.string.ui_step_screen_title)) { askForScreenSharing() }
        screenStep.button.text = getString(R.string.ui_step_screen_button)
        setupSteps.addView(screenStep.row)
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

        // "Claude is using your phone", while a controller event says someone is driving it.
        controllerText = text("", 14f, color(R.color.om_accent), bold = true).apply { visibility = View.GONE }
        card.addView(controllerText, margins(top = 6, start = 20))

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

    // 3b. The agent indicator: the owner's settings, changed only here (never by a server, API or protocol method;
    // agents can't tap OpenMob's own screens: the gate's own_packages rule).
    private fun buildIndicatorCard(): View {
        val prefs = ControlIndicator.prefs(this)
        val card = card()
        card.addView(cardTitle(getString(R.string.indicator_title)))
        card.addView(text(getString(R.string.indicator_help), 13f, color(R.color.om_grey)), margins(top = 4))

        fun switch(label: Int, on: Boolean, set: (Boolean) -> Unit): Switch = Switch(this).apply {
            text = getString(label)
            textSize = 15f
            setTextColor(color(R.color.om_text))
            isChecked = on
            minHeight = dp(48)
            setOnCheckedChangeListener { _, v -> set(v); ControlIndicator.refresh() }
        }
        card.addView(switch(R.string.indicator_border, prefs.border) { prefs.border = it }, margins(top = 12, width = match))

        card.addView(text(getString(R.string.indicator_color), 15f, color(R.color.om_text)), margins(top = 12))
        val swatches = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val dots = mutableListOf<Pair<String, View>>()
        fun mark() = dots.forEach { (key, v) ->
            (v.background as GradientDrawable).setStroke(dp(if (key == prefs.color) 3 else 1),
                if (key == prefs.color) color(R.color.om_text) else color(R.color.om_card_stroke))
        }
        for (preset in IndicatorColors.PRESETS) {
            val dot = View(this).apply {
                background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(preset.argb) }
                contentDescription = preset.label
                setOnClickListener { prefs.color = preset.key; mark(); ControlIndicator.refresh() }
            }
            dots.add(preset.key to dot)
            // a 36 dp dot in a 48 dp touch target
            swatches.addView(dot, LinearLayout.LayoutParams(dp(36), dp(36)).apply { marginEnd = dp(12); topMargin = dp(6); bottomMargin = dp(6) })
        }
        card.addView(swatches, margins(top = 4))
        card.addView(link(getString(R.string.indicator_custom)) {
            val input = input(getString(R.string.indicator_custom_hint)).apply {
                if (prefs.color.startsWith("#")) setText(prefs.color)
            }
            AlertDialog.Builder(this)
                .setTitle(R.string.indicator_custom_title)
                .setView(input)
                .setPositiveButton(R.string.ui_ok) { _, _ ->
                    val v = input.text.toString().trim().let { if (it.startsWith("#")) it else "#$it" }
                    if (IndicatorColors.valid(v)) { prefs.color = v; mark(); ControlIndicator.refresh() }
                    else toast(getString(R.string.indicator_custom_invalid))
                }
                .setNegativeButton(R.string.ui_cancel, null)
                .show()
        })
        mark()

        card.addView(switch(R.string.indicator_pulse, prefs.pulse) { prefs.pulse = it }, margins(top = 8, width = match))
        card.addView(text(getString(R.string.indicator_pulse_help), 13f, color(R.color.om_grey)), margins(top = 2))
        card.addView(switch(R.string.indicator_pill, prefs.pill) { prefs.pill = it }, margins(top = 12, width = match))

        card.addView(text(getString(R.string.indicator_corner), 15f, color(R.color.om_text)), margins(top = 12))
        val corners = listOf(PillCorner.TOP_RIGHT to R.string.indicator_corner_top_right,
            PillCorner.TOP_LEFT to R.string.indicator_corner_top_left,
            PillCorner.BOTTOM_RIGHT to R.string.indicator_corner_bottom_right,
            PillCorner.BOTTOM_LEFT to R.string.indicator_corner_bottom_left)
        val group = android.widget.RadioGroup(this).apply { orientation = android.widget.RadioGroup.VERTICAL }
        for ((corner, label) in corners) {
            group.addView(android.widget.RadioButton(this).apply {
                id = View.generateViewId()
                text = getString(label)
                textSize = 15f
                setTextColor(color(R.color.om_text))
                minHeight = dp(48)
                isChecked = prefs.corner == corner
                setOnCheckedChangeListener { _, on -> if (on) { prefs.corner = corner; ControlIndicator.refresh() } }
            })
        }
        card.addView(group, margins(top = 2, width = match))
        card.addView(button(getString(R.string.indicator_preview), Style.SECONDARY) { ControlIndicator.preview(this) },
            margins(top = 12, width = match))
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

    /** A small text button (Why?, Samsung's extra battery list, developer token). */
    private fun link(label: String, onClick: () -> Unit): TextView =
        text(label, 14f, color(R.color.om_accent), bold = true).apply {
            setPadding(0, dp(10), 0, dp(4))
            setOnClickListener { onClick() }
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
