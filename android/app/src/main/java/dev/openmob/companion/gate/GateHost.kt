package dev.openmob.companion.gate

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import dev.openmob.companion.AppState
import dev.openmob.companion.ApprovalKeys
import dev.openmob.companion.Approvals
import dev.openmob.companion.OpenMobAccessibilityService
import dev.openmob.companion.PhoneActions
import dev.openmob.companion.Settings
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Timer
import kotlin.concurrent.fixedRateTimer

/**
 * The gatekeeper on the phone: one [Gatekeeper] for the app, its signed owner policy on disk, and the
 * phone-owned approval prompt (docs/design/phone-gatekeeper.md). ConnectionService asks [admit] first for every
 * call; PhoneActions asks [reverify] right before each side effect.
 */
object GateHost {
    private const val FILE = "gatekeeper_policy.json"
    private const val QUIET_WAIT_MS = 1_000L

    @Volatile private var gate: Gatekeeper? = null
    private lateinit var app: Context
    private var saver: Timer? = null
    private val main = Handler(Looper.getMainLooper())
    /** Gate events for the server (ConnectionService sends them as `gate_event`). */
    @Volatile var sink: ((JSONObject) -> Unit)? = null
    @Volatile private var blockedLabelCache: Pair<Long, Map<String, String>>? = null

    val rulesVersion: Long get() = gk().rules.version

    fun init(context: Context): Gatekeeper {
        gate?.let { return it }
        synchronized(this) {
            gate?.let { return it }
            app = context.applicationContext
            val verifier = GateVerifier { m, s -> ApprovalKeys.verify(m, s) }
            val g = Gatekeeper(GateRules.builtIn(), { SystemClock.elapsedRealtime() }, verifier,
                               Settings(app).deviceId, ModeChange.verifier(verifier))
            g.onEvent = { id, state -> onEvent(id, state) }
            load(g)
            gate = g
            saver = fixedRateTimer("gate-save", daemon = true, initialDelay = 60_000, period = 60_000) { save() }
            refreshBanner()
            return g
        }
    }

    private fun gk(): Gatekeeper = gate ?: error("GateHost.init first")

    fun lastAppOrEmpty(): String = gate?.lastApp.orEmpty()

    /** Labels of the installed blocked apps (an app-info page names its app by label). */
    @Suppress("DEPRECATION")
    fun blockedLabels(pm: PackageManager): Map<String, String> {
        blockedLabelCache?.let { (at, labels) -> if (SystemClock.elapsedRealtime() - at < 600_000) return labels }
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val blocked = runCatching { pm.queryIntentActivities(launcher, 0).map { it.activityInfo.packageName } }
            .getOrDefault(emptyList()).filter { gk().rules.blocked(it) }
        return GateScreens.labels(pm, blocked).also { blockedLabelCache = SystemClock.elapsedRealtime() to it }
    }

    // --- the checkpoint -------------------------------------------------------------------------

    fun admit(service: OpenMobAccessibilityService?, method: String, params: Map<String, Any?>): Admission {
        val g = gk()
        if (method in g.rules.meta || service == null) return g.admit(method, params, GScreen(emptyList()))
        if (method in g.rules.read || method.startsWith("stream_")) {
            val screen = if (method == "screenshot" || method == "ui_elements") GateScreens.windowPackages(service) else GScreen(emptyList())
            return g.admit(method, params, screen)
        }
        val adm = g.admit(method, params, screen(service, method, params))
        if (adm.decision == "ask") prompt(service, adm)
        return adm
    }

    /** Right before the side effect (PhoneActions, under the gesture lock). Waits up to a second for a quiet screen
     *  when the action is risky. Returns the refusal, or null to go ahead. */
    fun reverify(service: OpenMobAccessibilityService, adm: Admission?): Admission? {
        if (adm == null || !adm.allowed) return null
        val g = gk()
        val deadline = SystemClock.uptimeMillis() + QUIET_WAIT_MS
        while (true) {
            val out = g.reverify(adm, screen(service, adm.method, adm.params))
            if (out.code != "gate_screen_unstable" || SystemClock.uptimeMillis() >= deadline) return out.takeIf { !it.allowed }
            SystemClock.sleep(50)
        }
    }

    private fun screen(service: OpenMobAccessibilityService, method: String, params: Map<String, Any?>): GScreen {
        val urls = mutableListOf<String>()
        if (method == "open_url") params["url"]?.toString()?.let(urls::add)
        val launch = if (method == "launch_app") listOfNotNull(params["package"]?.toString()) else emptyList()
        var screen = GateScreens.capture(service, urls, launch)
        if (method == "key" && params["name"]?.toString()?.uppercase() == "ENTER") {
            // ENTER in a browser's URL bar opens what's typed there: resolve it like open_url.
            GateScreen.focusedField(screen)?.node?.text?.trim()?.takeIf { GateText.looksLikeUrl(it) }?.let { text ->
                val url = if (GateText.urlScheme(text) == null) "https://$text" else text
                screen = GScreen(screen.windows, screen.keyguardLocked, screen.keyguardSecure, screen.quietMs,
                    screen.activity, screen.autofillService, mapOf(url to GateScreens.resolve(service.packageManager, url)),
                    screen.labels)
            }
        }
        return screen
    }

    // --- the phone-owned prompt (D5) ------------------------------------------------------------

    private fun prompt(service: OpenMobAccessibilityService, adm: Admission) {
        val point = when (adm.method) {
            "tap", "long_press" -> num(adm.params["x"]) to num(adm.params["y"])
            "swipe" -> num(adm.params["x1"]) to num(adm.params["y1"])
            else -> null
        }
        // The crop shows the owner the item and the price: the signature covers its hash.
        val crop = point?.let { (x, y) -> runCatching { PhoneActions(service).crop(x, y) }.getOrNull() }
        crop?.let { gk().setCrop(adm.approvalId, GateText.sha256(it)) }
        val hint = adm.params["hint"]?.toString()?.trim().orEmpty()
        val offer = adm.grantOffer
        main.post {
            Approvals.requestGate(app, adm.approvalId, adm.wording, hint, crop, offer?.let { grantText(it, adm) })
        }
    }

    private fun num(v: Any?): Int = (v as? Number)?.toInt() ?: v?.toString()?.toDoubleOrNull()?.toInt() ?: -1

    /** What "Always allow" would let anyone driving the phone do (D20, S4). Not offered for browser autofill. */
    private fun grantText(offer: GrantOffer, adm: Admission): Approvals.Always {
        val days = offer.durationMs / 86_400_000.0
        val span = if (days >= 2) "${days.toInt()} days" else "24 hours"
        val app = offer.binding.substringBefore('>')
        val text = "for $span, OpenMob (or a compromised server) could " + when (offer.kind) {
            "send" -> "send any $app message as you without asking"
            "post" -> "post anything on $app as you without asking"
            "delete" -> "delete anything in $app without asking"
            "pay" -> "pay for anything in $app without asking"
            "share" -> "share anything from $app to ${offer.binding.substringAfter('>')} without asking"
            "settings" -> "change this Settings page without asking"
            else -> "fill your saved passwords and cards into $app without asking"
        }
        return Approvals.Always(adm.pkg, offer.kind, days, text, offer.confirmTwice)
    }

    fun approvalMessage(id: String, grant: Boolean): String? = gk().approvalMessage(id, grant)

    /** The owner passed the screen lock: [signature] was made by the auth-bound key over [message]. */
    fun approve(id: String, message: String, signature: String): Boolean = gk().approve(id, message, signature).also {
        if (it) save()
    }

    fun deny(id: String) = gk().deny(id)
    fun cancel(id: String) = gk().cancel(id)

    private fun onEvent(id: String, state: String) {
        sink?.invoke(JSONObject().put("type", "gate_event").put("approval_id", id).put("state", state))
        if (state == "expired") main.post { Approvals.cancel(app, id) }
        if (state == "denied" && gk().suspended) main.post {
            dev.openmob.companion.ConnectionService.showResume()
            AppState.addEvent("status", "Paused: you denied 3 requests in 10 minutes. Open OpenMob and tap Resume to continue.")
        }
    }

    // --- owner policy --------------------------------------------------------------------------

    /** A permission change the owner approved (with the screen lock) on this phone. */
    fun applyModeChange(approvalId: String, taskId: String, action: Map<String, String>, timestamp: Long,
                        always: Boolean, signature: String): Int {
        val input = action["input"].orEmpty()
        if (ModeChange.turnsNeverOff(input)) dropNever()
        val proof = ModeChange.proof(approvalId, taskId, action, timestamp, always, signature)
        val n = ModeChange.records(input).count { gk().addPolicy(it, proof) }
        save()
        refreshBanner()
        return n
    }

    /** A tightening, from anywhere (the server's "never: off", the banner's Turn off, Stop). */
    fun dropNever() {
        if (gate == null) return
        if (gk().neverActive) {
            gk().revokeNeverOnly()
            save()
        }
        refreshBanner()
    }

    fun revokeAll() {
        if (gate == null) return
        gk().revokeAll()
        main.post { Approvals.clearGate(app) }
        save()
        refreshBanner()
    }

    fun resume() = gate?.resume()
    fun noteInstruction(text: String) = gate?.noteInstruction(text)
    val suspended: Boolean get() = gate?.suspended == true

    /** The `never` banner comes from the phone's own signed window, never from the server's claim. */
    fun refreshBanner() {
        val g = gate ?: return
        val left = g.neverRemaining
        AppState.setNeverUntil(if (left > 0) (System.currentTimeMillis() + left) / 1000 else 0, g.neverMoney)
    }

    /** policy_hash in the hello: which rules and owner policy this phone enforces (for the dashboard). */
    fun policyHash(): String {
        val g = gk()
        return GateText.sha256("rules:${g.rules.version}|ceiling:${g.ceiling}|never:${g.neverActive}:${g.neverMoney}").take(16)
    }

    // --- persistence (app-private; every record is re-verified on load) --------------------------

    @Synchronized fun save() {
        val g = gate ?: return
        val (entries, hwm) = g.persist()
        val out = JSONObject().put("hwm", hwm).put("entries", JSONArray(entries.map { e ->
            JSONObject().put("record", JSONObject(e.record)).put("message", e.proof.message)
                .put("signature", e.proof.signature).put("extra", JSONObject(e.proof.extra))
                .put("issued", e.issued).apply { e.remaining?.let { put("remaining", it) } }
        }))
        runCatching { File(app.filesDir, FILE).writeText(out.toString()) }
    }

    private fun load(g: Gatekeeper) {
        val text = runCatching { File(app.filesDir, FILE).readText() }.getOrNull() ?: return
        runCatching {
            val o = JSONObject(text)
            val list = o.optJSONArray("entries") ?: JSONArray()
            val entries = (0 until list.length()).map { i ->
                val e = list.getJSONObject(i)
                val r = e.getJSONObject("record")
                val record = r.keys().asSequence().associateWith { k -> r.get(k).let { if (it is Int) it.toLong() else it } }
                val x = e.optJSONObject("extra") ?: JSONObject()
                PolicyEntry(record, PolicyProof(e.getString("message"), e.getString("signature"),
                    x.keys().asSequence().associateWith { x.getString(it) }), e.optLong("issued"),
                    if (e.has("remaining")) e.getLong("remaining") else null)
            }
            val loaded = g.restore(entries, o.optLong("hwm"))
            if (loaded < entries.size) AppState.addEvent("status",
                "Some saved permissions couldn't be verified or had expired, so agents ask as usual.")
        }
    }
}
