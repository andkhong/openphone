package dev.openmob.companion

import android.app.Activity
import android.content.Intent
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * Review one approval request (see [Approvals]): who is asking and what. Approving needs the
 * phone's screen lock, the same check as pairing, so the AI driving the phone can't approve its
 * own purchase or send. Denying needs nothing.
 *
 * FLAG_SECURE keeps this screen out of screenshots and the live view, so the agent can't read it.
 */
class ApprovalActivity : Activity() {
    private companion object {
        const val APPROVERS = BiometricManager.Authenticators.BIOMETRIC_STRONG or
            BiometricManager.Authenticators.DEVICE_CREDENTIAL
    }

    private var requestId = ""
    private var shown: Approvals.Request? = null // kept after it's answered, to keep showing it
    private var unsubscribe: (() -> Unit)? = null
    private var prompt: CancellationSignal? = null

    private lateinit var questionText: TextView
    private lateinit var requesterText: TextView
    private lateinit var statusText: TextView
    private lateinit var approveButton: Button
    private lateinit var denyButton: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        requestWindowFeature(Window.FEATURE_NO_TITLE)
        setContentView(buildContent())
        open(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        open(intent)
    }

    override fun onResume() {
        super.onResume()
        unsubscribe = AppState.listen { render() }
        render()
    }

    override fun onPause() {
        unsubscribe?.invoke()
        unsubscribe = null
        super.onPause()
    }

    override fun onDestroy() {
        prompt?.cancel()
        prompt = null
        super.onDestroy()
    }

    private fun open(intent: Intent?) {
        prompt?.cancel()
        prompt = null
        requestId = intent?.getStringExtra(Approvals.EXTRA_ID).orEmpty()
        shown = Approvals.get(requestId)
        statusText.visibility = View.GONE
        render()
    }

    private fun render() {
        Approvals.get(requestId)?.let { shown = it }
        val request = shown
        val open = Approvals.get(requestId) != null
        questionText.text = request?.shownQuestion ?: getString(R.string.approval_gone)
        requesterText.text = request?.let { getString(R.string.approval_requester, it.requester) }.orEmpty()
        requesterText.visibility = if (request == null) View.GONE else View.VISIBLE
        if (!open && request != null) showStatus(getString(R.string.approval_gone))
        listOf(approveButton, denyButton).forEach {
            it.isEnabled = open
            it.alpha = if (open) 1f else 0.45f
        }
    }

    private fun onApprove() {
        val request = Approvals.get(requestId) ?: return render()
        val lock = getSystemService(BiometricManager::class.java)?.canAuthenticate(APPROVERS)
        if (lock != BiometricManager.BIOMETRIC_SUCCESS) {
            showStatus(getString(R.string.approval_need_lock))
            return
        }
        prompt?.cancel()
        val cancel = CancellationSignal().also { prompt = it }
        // The approval key only signs once this prompt is passed: that signature is what the server
        // trusts, so nothing else on the phone (or holding its device key) can approve.
        val signer = ApprovalKeys.signer()
        val built = BiometricPrompt.Builder(this)
            .setTitle(getString(R.string.approval_prompt_title))
            .setSubtitle(getString(R.string.approval_requester, request.requester))
            .setDescription(request.shownQuestion)
            .setAllowedAuthenticators(APPROVERS) // no negative button allowed with DEVICE_CREDENTIAL
            .build()
        val callback = object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                    if (prompt === cancel) prompt = null
                    val unlocked = result?.cryptoObject?.signature
                    if (unlocked == null) {
                        send(approved = true) // no approval key on this phone: the server treats it as an older app
                        return
                    }
                    val now = System.currentTimeMillis() / 1000
                    val signature = runCatching { ApprovalKeys.sign(unlocked, request.id, request.taskId, now) }.getOrNull()
                    if (signature == null) {
                        showStatus(getString(R.string.approval_not_confirmed))
                        return
                    }
                    send(approved = true, signature = signature, timestamp = now)
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                    if (prompt === cancel) prompt = null
                    if (!isDestroyed) showStatus(getString(R.string.approval_not_confirmed))
                }
                // onAuthenticationFailed (one wrong finger) needs nothing: the prompt stays up.
            }
        if (signer != null) {
            built.authenticate(BiometricPrompt.CryptoObject(signer), cancel, mainExecutor, callback)
        } else {
            built.authenticate(cancel, mainExecutor, callback)
        }
    }

    private fun send(approved: Boolean, signature: String? = null, timestamp: Long = 0) {
        if (Approvals.answer(this, requestId, approved, signature, timestamp)) {
            Toast.makeText(
                this, if (approved) R.string.approval_sent_approved else R.string.approval_sent_denied, Toast.LENGTH_SHORT,
            ).show()
            finish()
            return
        }
        // Still pending means the answer couldn't be sent; otherwise it expired meanwhile.
        showStatus(getString(if (Approvals.get(requestId) != null) R.string.approval_offline else R.string.approval_gone))
        render()
    }

    private fun showStatus(message: String) {
        statusText.text = message
        statusText.visibility = View.VISIBLE
    }

    // ---------------------------------------------------------------- layout

    private fun buildContent(): View {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(getColor(R.color.om_card), 20, stroke = getColor(R.color.om_question))
            setPadding(dp(20), dp(20), dp(20), dp(20))
        }
        card.addView(text(getString(R.string.approval_label), 12f, getColor(R.color.om_question), bold = true))
        questionText = text("", 20f, getColor(R.color.om_text), bold = true)
        card.addView(questionText, margins(top = 6))
        requesterText = text("", 14f, getColor(R.color.om_text_secondary))
        card.addView(requesterText, margins(top = 6))
        card.addView(text(getString(R.string.approval_explain), 13f, getColor(R.color.om_text_tertiary)), margins(top = 12))

        approveButton = button(getString(R.string.approval_approve), getColor(R.color.om_accent), getColor(R.color.om_on_accent)) {
            onApprove()
        }.apply {
            filterTouchesWhenObscured = true // no taps through an overlay drawn on top
            // Android 14+: hidden from accessibility services, including the one the agent drives.
            if (Build.VERSION.SDK_INT >= 34) setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES)
        }
        denyButton = button(getString(R.string.approval_deny), getColor(R.color.om_input_bg), getColor(R.color.om_error)) {
            send(approved = false)
        }
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        buttons.addView(denyButton, LinearLayout.LayoutParams(0, dp(48), 1f))
        buttons.addView(approveButton, LinearLayout.LayoutParams(0, dp(48), 1f).apply { marginStart = dp(10) })
        card.addView(buttons, margins(top = 18, width = ViewGroup.LayoutParams.MATCH_PARENT))

        statusText = text("", 13f, getColor(R.color.om_error)).apply { visibility = View.GONE }
        card.addView(statusText, margins(top = 12))

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(24))
            addView(card, margins(width = ViewGroup.LayoutParams.MATCH_PARENT))
        }
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(getColor(R.color.om_bg))
            addView(column)
        }
        // Edge-to-edge (targetSdk 35): keep the card clear of the system bars.
        scroll.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        return scroll
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + 0.5f).toInt()

    private fun rounded(fill: Int, radiusDp: Int, stroke: Int? = null) = GradientDrawable().apply {
        cornerRadius = dp(radiusDp).toFloat()
        setColor(fill)
        stroke?.let { setStroke(dp(1), it) }
    }

    private fun margins(top: Int = 0, width: Int = ViewGroup.LayoutParams.WRAP_CONTENT) =
        LinearLayout.LayoutParams(width, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(top) }

    private fun text(value: String, sizeSp: Float, color: Int, bold: Boolean = false) = TextView(this).apply {
        text = value
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
        setTextColor(color)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setLineSpacing(0f, 1.15f)
    }

    private fun button(label: String, fill: Int, textColor: Int, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        stateListAnimator = null
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        setTextColor(textColor)
        background = rounded(fill, 12)
        setOnClickListener { onClick() }
    }
}
