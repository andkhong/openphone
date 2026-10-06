package dev.openmob.companion

import android.app.Activity
import android.app.AlertDialog
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
import android.widget.CheckBox
import android.widget.ImageView
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
 *
 * Kinds (docs/phone-protocol.md): a risky action, a step (ask-every-step mode), a plan (plan-first
 * mode) or a mode_change (agents would ask less on this phone). "Always allow this" (off by default)
 * also grants the offered trusted entry; for payments it asks a second time. It is signed too. A
 * mode_change that also allows purchases ("Also allow purchases" with Don't ask) asks a second time
 * as well, before the screen lock.
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

    private lateinit var labelText: TextView
    private lateinit var explainText: TextView
    private lateinit var alwaysBox: CheckBox
    private lateinit var questionText: TextView
    private lateinit var actionText: TextView
    private lateinit var requesterText: TextView
    private lateinit var cropView: ImageView
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

    /** A gate prompt the system interrupted (request id, "always" ticked): shown again when the screen is back. */
    private var resumeAuth: Pair<String, Boolean>? = null

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        val (id, always) = resumeAuth ?: return
        if (!hasFocus || prompt != null) return
        resumeAuth = null
        Approvals.get(id)?.takeIf { it.gate && id == requestId }?.let { authenticate(it, always) }
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
        // The owner sees this gate prompt now: only prompts actually shown count toward the prompt limit.
        if (open && request?.gate == true && !isFinishing) dev.openmob.companion.gate.GateHost.markShown(requestId)
        questionText.text = request?.shownQuestion ?: getString(R.string.approval_gone)
        val kind = request?.kind ?: "action"
        labelText.text = getString(when (kind) {
            "mode_change" -> R.string.approval_label_mode
            "plan" -> R.string.approval_label_plan
            "step" -> R.string.approval_label_step
            else -> R.string.approval_label
        })
        explainText.text = getString(when {
            request?.allowsMoney == true -> R.string.approval_explain_mode_money
            request?.gate == true -> R.string.gate_explain
            kind == "mode_change" -> R.string.approval_explain_mode
            kind == "plan" -> R.string.approval_explain_plan
            else -> R.string.approval_explain
        })
        val always = request?.always
        alwaysBox.visibility = if (always != null && open) View.VISIBLE else View.GONE
        if (always != null) alwaysBox.text = (if (always.kind == "permission") getString(R.string.gate_always_for, appLabel(always.app))
            else getString(R.string.approval_always)) + "\n${always.text}"
        requesterText.text = request?.let {
            if (it.gate) (if (it.serverSays.isEmpty()) "" else getString(R.string.gate_server_says, it.serverSays))
            else getString(R.string.approval_requester, it.requester)
        }.orEmpty()
        val crop = request?.crop?.let { android.graphics.BitmapFactory.decodeByteArray(it, 0, it.size) }
        cropView.setImageBitmap(crop)
        cropView.visibility = if (crop == null) View.GONE else View.VISIBLE
        requesterText.visibility = if (requesterText.text.isNullOrEmpty()) View.GONE else View.VISIBLE
        // What exactly will happen, built here from the action's own fields (what the signature binds).
        val line = request?.actionLine.orEmpty()
        actionText.text = if (line.isEmpty()) "" else getString(R.string.approval_action, line)
        actionText.visibility = if (line.isEmpty()) View.GONE else View.VISIBLE
        val blocker = request?.let { blocker(it) }
        if (!open && request != null) showStatus(getString(R.string.approval_gone))
        else if (blocker != null) showStatus(blocker)
        denyButton.isEnabled = open
        denyButton.alpha = if (open) 1f else 0.45f
        // A phone that can't sign this approval can only deny it.
        approveButton.isEnabled = open && blocker == null
        approveButton.alpha = if (approveButton.isEnabled) 1f else 0.45f
        approveButton.visibility = if (blocker != null) View.GONE else View.VISIBLE
    }

    /** Why this request can't be approved on this phone (it can still be denied), or null. */
    private fun blocker(request: Approvals.Request): String? = when {
        !request.canApprove -> request.cannotApproveReason.ifEmpty { getString(R.string.approval_cannot) }
        // The action shown must be the action signed: a request whose digest doesn't match its own
        // fields is malformed (or lying), so it can't be approved.
        request.digest.isNotEmpty() && !request.consistent -> getString(R.string.approval_mismatch)
        request.signatureRequired && request.digest.isEmpty() -> getString(R.string.approval_mismatch)
        else -> null
    }

    private fun onApprove() {
        val request = Approvals.get(requestId) ?: return render()
        blocker(request)?.let { return showStatus(it) }
        // Don't ask including purchases: a second, explicit yes for the money part before the screen lock.
        if (request.allowsMoney) {
            AlertDialog.Builder(this)
                .setTitle(R.string.approval_money_confirm_title)
                .setMessage(R.string.approval_money_confirm_body)
                .setPositiveButton(R.string.approval_money_confirm_yes) { _, _ -> authenticate(request, false) }
                .setNegativeButton(R.string.approval_money_confirm_no, null)
                .show()
            return
        }
        val always = request.always
        // "Always allow" for payments needs a second, explicit yes before the screen lock.
        if (alwaysBox.isChecked && always != null && always.confirmTwice) {
            AlertDialog.Builder(this)
                .setTitle(R.string.approval_always_confirm_title)
                .setMessage(getString(R.string.approval_always_confirm_body, always.text))
                .setPositiveButton(R.string.approval_always_confirm_yes) { _, _ -> authenticate(request, true) }
                .setNegativeButton(R.string.approval_always_confirm_no) { _, _ ->
                    alwaysBox.isChecked = false
                    authenticate(request, false)
                }
                .show()
            return
        }
        authenticate(request, alwaysBox.isChecked && always != null)
    }

    private fun authenticate(request: Approvals.Request, grantAlways: Boolean) {
        val lock = getSystemService(BiometricManager::class.java)?.canAuthenticate(APPROVERS)
        if (lock != BiometricManager.BIOMETRIC_SUCCESS) {
            showStatus(getString(R.string.approval_need_lock))
            return
        }
        prompt?.cancel()
        val cancel = CancellationSignal().also { prompt = it }
        // The approval key only signs once this prompt is passed: that signature is what the server
        // trusts, so nothing else on the phone (or holding its device key) can approve.
        // Gate prompts are verified on this phone alone (GateHost), so the key is made here if the phone never got
        // one: developer-token pairing doesn't create it (only account pairing and its hello do).
        val signer = ApprovalKeys.signer() ?: if (request.gate && ApprovalKeys.publicKey() != null) ApprovalKeys.signer() else null
        if (signer == null && (request.signatureRequired || request.gate)) {
            // A gate permit is minted only from the screen-lock signature: there is no unsigned fallback (D5).
            // Without the key there is nothing the server would accept: never send an unsigned yes.
            showStatus(getString(R.string.approval_no_key))
            return
        }
        val built = BiometricPrompt.Builder(this)
            .setTitle(getString(when (request.kind) {
                "mode_change" -> R.string.approval_prompt_title_mode
                "plan" -> R.string.approval_prompt_title_plan
                else -> R.string.approval_prompt_title
            }))
            .setSubtitle(getString(R.string.approval_requester, request.requester))
            .setDescription(listOf(request.shownQuestion, request.actionLine).filter { it.isNotEmpty() }
                .joinToString("\n"))
            .setAllowedAuthenticators(APPROVERS) // no negative button allowed with DEVICE_CREDENTIAL
            .build()
        val callback = object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                    if (prompt === cancel) prompt = null
                    val unlocked = result?.cryptoObject?.signature
                    if (request.gate) {
                        // Sign the gatekeeper's own message (this device, this prompt, this exact action and crop),
                        // which it verifies before minting the one-shot permit.
                        val message = dev.openmob.companion.gate.GateHost.approvalMessage(request.id, grantAlways)
                        val signature = if (unlocked == null || message == null) null
                        else runCatching { ApprovalKeys.signRaw(unlocked, message) }.getOrNull()
                        if (signature == null || !dev.openmob.companion.gate.GateHost.approve(request.id, message!!, signature)) {
                            showStatus(getString(if (message == null) R.string.approval_gone else R.string.approval_not_confirmed))
                            return
                        }
                        Approvals.answerGate(this@ApprovalActivity, request.id, true)
                        Toast.makeText(this@ApprovalActivity, R.string.approval_sent_approved, Toast.LENGTH_SHORT).show()
                        finish()
                        return
                    }
                    if (unlocked == null) {
                        // No approval key: only a server that doesn't require signatures (token/dev mode)
                        // takes an unsigned yes; accounts mode refused this above.
                        if (request.signatureRequired) showStatus(getString(R.string.approval_no_key))
                        else send(approved = true)
                        return
                    }
                    val now = System.currentTimeMillis() / 1000
                    val signature = runCatching {
                        ApprovalKeys.sign(unlocked, request.id, request.taskId, request.digest, now, grantAlways)
                    }.getOrNull()
                    if (signature == null) {
                        showStatus(getString(R.string.approval_not_confirmed))
                        return
                    }
                    send(approved = true, signature = signature, timestamp = now, always = grantAlways)
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                    if (prompt === cancel) prompt = null
                    if (isDestroyed) return
                    if (request.gate) {
                        when (PromptOutcome.classify(errorCode)) {
                            // The owner cancelled the screen lock themselves: that's their no.
                            PromptOutcome.Kind.DECLINED -> return send(approved = false)
                            // The system interrupted it (a notification, a call, the screen going off): keep the
                            // request and bring the prompt back once this screen has focus again.
                            PromptOutcome.Kind.INTERRUPTED -> {
                                resumeAuth = request.id to grantAlways
                                showStatus(getString(R.string.gate_prompt_interrupted))
                                return
                            }
                        }
                    }
                    showStatus(getString(R.string.approval_not_confirmed))
                }
                // onAuthenticationFailed (one wrong finger) needs nothing: the prompt stays up.
            }
        if (signer != null) {
            built.authenticate(BiometricPrompt.CryptoObject(signer), cancel, mainExecutor, callback)
        } else {
            built.authenticate(cancel, mainExecutor, callback)
        }
    }

    private fun send(approved: Boolean, signature: String? = null, timestamp: Long = 0, always: Boolean = false) {
        val request = Approvals.get(requestId)
        if (Approvals.answer(this, requestId, approved, signature, timestamp, always)) {
            // A permission change the owner just signed becomes this phone's own policy (the gatekeeper enforces
            // the ceiling and `never`; the server's copy is only for its dashboard).
            if (approved && signature != null && request?.kind == "mode_change") {
                dev.openmob.companion.gate.GateHost.applyModeChange(request.id, request.taskId, request.action,
                                                                    timestamp, always, signature)
            }
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

    @Suppress("DEPRECATION")
    private fun appLabel(pkg: String): String =
        runCatching { packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString() }
            .getOrDefault(pkg)

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
        labelText = text(getString(R.string.approval_label), 12f, getColor(R.color.om_question), bold = true)
        card.addView(labelText)
        questionText = text("", 20f, getColor(R.color.om_text), bold = true)
        card.addView(questionText, margins(top = 6))
        // The gatekeeper's crop of the target (with a crosshair): what exactly would be tapped, item and price.
        cropView = ImageView(this).apply { adjustViewBounds = true; visibility = View.GONE }
        card.addView(cropView, margins(top = 10, width = ViewGroup.LayoutParams.MATCH_PARENT))
        actionText = text("", 15f, getColor(R.color.om_text))
        card.addView(actionText, margins(top = 8))
        requesterText = text("", 14f, getColor(R.color.om_text_secondary))
        card.addView(requesterText, margins(top = 6))
        explainText = text(getString(R.string.approval_explain), 13f, getColor(R.color.om_text_tertiary))
        card.addView(explainText, margins(top = 12))
        // Off by default: granting more than this one action is a deliberate choice.
        alwaysBox = CheckBox(this).apply {
            isChecked = false
            setTextColor(getColor(R.color.om_text))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            filterTouchesWhenObscured = true
            if (Build.VERSION.SDK_INT >= 34) setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES)
            visibility = View.GONE
        }
        card.addView(alwaysBox, margins(top = 12, width = ViewGroup.LayoutParams.MATCH_PARENT))

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
