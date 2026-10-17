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
import dev.openmob.companion.PermissionOcr
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
    val actMethods: Set<String> get() = gk().rules.act

    fun init(context: Context): Gatekeeper {
        gate?.let { return it }
        synchronized(this) {
            gate?.let { return it }
            app = context.applicationContext
            val verifier = GateVerifier { m, s -> ApprovalKeys.verify(m, s) }
            val g = Gatekeeper(GateRules.builtIn(), { SystemClock.elapsedRealtime() }, verifier,
                               Settings(app).deviceId, ModeChange.verifier(verifier),
                               GateVerifier { m, s -> dev.openmob.companion.TapKeys.verify(m, s) })
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

    /** A blocked or unidentified app is in some window right now: reads and the live view must not see the screen.
     *  Kept current by window events (OpenMobAccessibilityService) and checked again around every capture. */
    @Volatile var protectedVisible = false
        private set

    fun refreshProtected(service: OpenMobAccessibilityService?): Boolean {
        val g = gate ?: return false
        // No accessibility service: the phone can't see what's on screen, so it fails closed.
        if (service == null) { protectedVisible = true; return true }
        var screen = GateScreens.windowPackages(service)
        // A window with no package and no readable tree may be a permission dialog Android hides: shown once the
        // phone's OCR finds its title (black until the first background read, a fraction of a second).
        g.ocrCandidate(screen)?.takeIf { it.pkg.isEmpty() }?.let { screen = screen.withOcr(PermissionOcr.cached(service, it.id)) }
        val why = g.hidden(screen)
        protectedVisible = why.isNotEmpty()
        logRead("live", g, screen, why)
        return protectedVisible
    }

    @Volatile private var lastProtectedCheck = 0L

    /** For the live view's encoder thread: the flag, re-read from the windows at most every 150 ms. */
    /** A system dialog the phone can't read that a task is waiting on (its message), or null. */
    @Volatile var ownerDialog: String? = null
        private set
    /** Told when that dialog closes (ConnectionService: owner_dialog closed, drop the notification). */
    @Volatile var onOwnerDialogClosed: (() -> Unit)? = null

    /** The hidden permission dialog as the gate reads it from OCR (its text lines and buttons), or null. */
    fun ocrNodes(service: OpenMobAccessibilityService): List<GNode>? {
        val g = gk()
        val base = GateScreens.dialogScreen(service)
        if (g.ocrCandidate(base) == null) return null
        val cand = g.ocrCandidate(base) ?: return null
        val read = g.ocrPreview(base.withOcr(PermissionOcr.read(service, cand.id) ?: return null))
        return read.windows.firstOrNull { it.root?.cls == "OCRDialog" }?.root?.children
    }

    /** The package of a hidden permission dialog the phone has identified from its last OCR reading (non-blocking:
     *  the cached reading only), or null. */
    fun ocrDialogPackage(service: OpenMobAccessibilityService): String? {
        val g = gate ?: return null
        val base = GateScreens.dialogScreen(service)
        val cand = g.ocrCandidate(base) ?: return null
        val lines = PermissionOcr.cached(service, cand.id) ?: return null
        return g.ocrPreview(base.withOcr(lines)).windows.firstOrNull { it.root?.cls == "OCRDialog" }?.pkg?.ifEmpty { null }
    }

    @Volatile private var lastReadLog = ""

    /** Diagnostics (logcat OpenMobGate): how a read was gated around a window the phone can't read. Only the dialog's
     *  title, never other screen text; logged when the decision changes. */
    private fun logRead(what: String, g: Gatekeeper, screen: GScreen, why: String) {
        val cand = g.ocrCandidate(screen) ?: if (why.isEmpty()) return else null
        val dialog = g.ocrWindow(screen)
        val title = dialog?.root?.children?.firstOrNull { it.id == "permission_message" }?.text.orEmpty().take(120)
        val line = "read $what: candidate=${cand?.id}/${cand?.pkg?.ifEmpty { "-" }} since=${cand?.since} " +
            "event=${screen.eventPackage.ifEmpty { "-" }}@${screen.eventAt} ocr=${screen.ocr?.size ?: "none"} " +
            "titleSeen=${g.ocrTitle(screen)} dialog=${dialog?.id}/${dialog?.pkg} title=\"$title\" " +
            (if (why.isEmpty()) "shown" else "black($why)")
        if (line != lastReadLog) { lastReadLog = line; android.util.Log.i(PermissionOcr.TAG, line) }
    }

    /** The admitted action's gesture completed (PhoneActions): the gate may record what it changed. */
    fun completed(adm: Admission?) {
        PermissionOcr.invalidate() // what it read may no longer be on screen (review M1)
        if (adm != null) runCatching { gk().completed(adm) }
    }

    /** The system Settings app, or a package Settings hands a page to (Samsung's Device care): `open_settings` opens
     *  nothing else (review L5). */
    fun isSettingsPackage(pkg: String): Boolean = gk().rules.settingsPkg.containsMatchIn(pkg)

    /** A task hit an unreadable system dialog (needs_owner_dialog): watch for it to close. */
    fun ownerDialogOpened(message: String) { ownerDialog = message; dev.openmob.companion.ControlIndicator.refresh() }

    /** On window events: has the dialog a task is waiting on gone? */
    fun refreshOwnerDialog(service: OpenMobAccessibilityService) {
        val g = gate ?: return
        if (ownerDialog == null) return
        if (runCatching { g.ownerDialog(GateScreens.dialogScreen(service)) }.getOrNull() == null) {
            ownerDialog = null
            onOwnerDialogClosed?.invoke()
            dev.openmob.companion.ControlIndicator.refresh()
        }
    }

    fun protectedNow(): Boolean {
        val now = SystemClock.uptimeMillis()
        if (now - lastProtectedCheck >= 150) {
            lastProtectedCheck = now
            runCatching { refreshProtected(OpenMobAccessibilityService.instance) }.onFailure { protectedVisible = true }
        }
        return protectedVisible
    }

    // --- the checkpoint -------------------------------------------------------------------------

    /** After the owner's Stop on the agent indicator: acts are refused until the task ends (docs/phone-protocol.md,
     *  task_stop). */
    val ownerStop = dev.openmob.companion.OwnerStopHold()

    /** The owner's Stop on the pill: acts are held, and every per-task permission goes (Gatekeeper.stopTasks). */
    fun ownerStopped() {
        ownerStop.stop(SystemClock.uptimeMillis())
        gate?.stopTasks()
        main.post { Approvals.clearGate(app) }
        save()
    }

    private fun stoppedRefusal(method: String, params: Map<String, Any?>) = Admission("refuse", code = "gate_owner_stopped",
        rule = "builtin:owner_stopped", message = "The owner tapped Stop on the phone: this task is stopped. Call done.",
        method = method, params = params)

    fun admit(service: OpenMobAccessibilityService?, method: String, params: Map<String, Any?>): Admission {
        val g = gk()
        if (method in g.rules.act && ownerStop.blocks(SystemClock.uptimeMillis())) return stoppedRefusal(method, params)
        // Without the accessibility service the phone sees nothing: every act is refused (gate_unavailable).
        if (method in g.rules.task) { // task approval: the phone words the prompt from the request itself
            val adm = g.admit(method, params, GScreen(emptyList()))
            if (adm.decision == "ask" && service != null) prompt(service, adm)
            return adm
        }
        if (method in g.rules.meta || service == null) return g.admit(method, params, GScreen(emptyList(), available = service != null))
        if (method in g.rules.read || method.startsWith("stream_")) {
            var screen = if (method == "screenshot" || method == "ui_elements" || method == "wait_for" || method == "stream_start")
                GateScreens.windowPackages(service) else GScreen(emptyList())
            // A window with no package and no readable tree may be a permission dialog Android hides: OCR it.
            g.ocrCandidate(screen)?.takeIf { it.pkg.isEmpty() }?.let { w ->
                screen = screen.withOcr(runCatching { PermissionOcr.read(service, w.id) }.getOrNull())
            }
            return g.admit(method, params, screen).also { logRead(method, g, screen, if (it.decision == "redact") it.rule else "") }
        }
        var adm = g.admit(method, params, screen(service, method, params))
        // A window that is briefly unreadable or missing from the list (a live screen redrawing): look again for a
        // moment before refusing. Refusals have no side effects, so a retry costs nothing but time.
        val until = SystemClock.uptimeMillis() + TRANSIENT_MS
        while (adm.code == "gate_screen_unstable" && adm.rule in TRANSIENT && SystemClock.uptimeMillis() < until) {
            SystemClock.sleep(100)
            adm = g.admit(method, params, screen(service, method, params))
        }
        // The agent aimed at something under OpenMob's Stop pill: if the gate would accept the gesture without the pill
        // there, the pill moves to another visible corner and the gesture is judged again on the screen as it is now
        // (docs/design/control-indicator.md, "The pill dodges"). Otherwise, or if no corner is free, it stays refused.
        if (adm.rule == "builtin:own_indicator") adm = dodgePill(service, g, method, params) ?: adm
        if (adm.decision == "ask") prompt(service, adm)
        return adm
    }

    private fun gesturePoints(params: Map<String, Any?>): List<Pair<Double, Double>> {
        fun n(k: String) = (params[k] as? Number)?.toDouble() ?: params[k]?.toString()?.toDoubleOrNull()
        return listOf("x" to "y", "x1" to "y1", "x2" to "y2").mapNotNull { (a, b) ->
            val x = n(a); val y = n(b)
            if (x != null && y != null) x to y else null
        }
    }

    private fun dodgePill(service: OpenMobAccessibilityService, g: Gatekeeper, method: String, params: Map<String, Any?>): Admission? {
        if (!g.admitsWithoutPill(method, params, screen(service, method, params))) return null
        val points = gesturePoints(params)
        if (!dev.openmob.companion.ControlIndicator.dodge(points)) return null
        // The accessibility window list catches up with the moved window a moment later.
        val until = SystemClock.uptimeMillis() + 800
        var now = screen(service, method, params)
        fun onPill(s: GScreen) = points.any { (x, y) ->
            GateScreen.hitTest(g.rules, s, x, y)?.window?.let { GateScreen.ownOverlay(g.rules, it) } == true
        }
        while (onPill(now) && SystemClock.uptimeMillis() < until) {
            SystemClock.sleep(50)
            now = screen(service, method, params)
        }
        android.util.Log.i(PermissionOcr.TAG, "indicator: the Stop pill moved out of the way of an agent's $method")
        return g.admit(method, params, now)
    }

    private const val STILL_MS = 150L
    private const val TRANSIENT_MS = 600L
    private val TRANSIENT = setOf("builtin:unknown_window", "builtin:no_window", "builtin:focus_unread", "builtin:no_field")

    /** Right before the side effect (PhoneActions, under the gesture lock). Waits up to a second for a quiet screen
     *  when the action is risky. Returns the refusal, or null to go ahead. */
    fun reverify(service: OpenMobAccessibilityService, adm: Admission?): Admission? {
        if (adm == null || !adm.allowed) return null
        // An act already admitted when the owner tapped Stop never runs.
        if (ownerStop.blocks(SystemClock.uptimeMillis())) return stoppedRefusal(adm.method, adm.params)
        val g = gk()
        val deadline = SystemClock.uptimeMillis() + QUIET_WAIT_MS
        var focused = true
        while (SystemClock.uptimeMillis() < deadline) { // a window that just appeared reaches the list first (B7)
            focused = runCatching { service.windows.any { it.isFocused } }.getOrDefault(false)
            // Window changes only: content updating inside a window (live odds, a clock) doesn't hold this up.
            val wait = g.settleMs(adm, SystemClock.uptimeMillis() - service.lastWindowChangeAt, focused)
            if (wait <= 0) break
            SystemClock.sleep(minOf(wait, 50L, deadline - SystemClock.uptimeMillis()).coerceAtLeast(1))
        }
        if (!focused && adm.method in Gatekeeper.GESTURES) return g.unsettled(adm)
        while (true) {
            var now = screen(service, adm.method, adm.params, fresh = true)
            if (adm.strict) {
                // D5's stillness is the target's: the same element, bounds and window over STILL_MS, numbers blanked.
                // The rest of the screen may keep changing.
                SystemClock.sleep(STILL_MS)
                val later = screen(service, adm.method, adm.params, fresh = true)
                now = later.withQuiet(if (g.stableKey(adm, now) == g.stableKey(adm, later)) 60_000L else 0L)
            }
            val out = g.reverify(adm, now)
            // Just after a prompt closed (B1): wait out the short cooldown rather than refuse an honest action.
            if (out.rule == "builtin:prompt_cooldown" && !g.promptOpen && SystemClock.uptimeMillis() < deadline + 1_200) {
                SystemClock.sleep(100)
                continue
            }
            if (out.code != "gate_screen_unstable" || SystemClock.uptimeMillis() >= deadline) return out.takeIf { !it.allowed }
            SystemClock.sleep(50)
        }
    }

    private fun screen(service: OpenMobAccessibilityService, method: String, params: Map<String, Any?>, fresh: Boolean = false): GScreen {
        val urls = mutableListOf<String>()
        if (method == "open_url") params["url"]?.toString()?.let(urls::add)
        val launch = if (method == "launch_app") listOfNotNull(params["package"]?.toString()) else emptyList()
        fun n(k: String) = (params[k] as? Number)?.toDouble() ?: params[k]?.toString()?.toDoubleOrNull()
        val points = listOf("x" to "y", "x1" to "y1", "x2" to "y2").mapNotNull { (a, b) ->
            val x = n(a); val y = n(b)
            if (x != null && y != null) x to y else null
        }
        var screen = GateScreens.capture(service, urls, launch, points)
        // A permission dialog Android hides from accessibility: read it with the phone's own OCR (the gate judges it).
        // The re-check right before an act never reuses a cached reading (review M1).
        gk().ocrCandidate(screen)?.let { screen = screen.withOcr(PermissionOcr.read(service, it.id, fresh)) }
        if (method == "key" && params["name"]?.toString()?.uppercase() == "ENTER") {
            // ENTER in a browser's URL bar opens what's typed there: resolve it like open_url.
            GateScreen.focusedField(screen)?.node?.text?.trim()?.takeIf { GateText.looksLikeUrl(it) }?.let { text ->
                val url = if (GateText.urlScheme(text) == null) "https://$text" else text
                screen = GScreen(screen.windows, screen.keyguardLocked, screen.keyguardSecure, screen.quietMs,
                    screen.activity, screen.autofillService, mapOf(url to GateScreens.resolve(service.packageManager, url)),
                    screen.labels, screen.focusExpected, screen.focusWindow, screen.focusNode, screen.focusAncestors, eventPackage = screen.eventPackage, eventText = screen.eventText,
                    owner = screen.owner, ocr = screen.ocr, eventAt = screen.eventAt)
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
        // A task prompt names who asked (as the server says: it's shown on the labeled "Server says" line).
        val hint = if (adm.kind == "task") "Requested by ${(adm.params["source"] ?: adm.params["task_source"])?.toString()?.take(60).orEmpty().ifEmpty { "an agent" }}"
            else adm.params["hint"]?.toString()?.trim().orEmpty()
        val offer = adm.grantOffer
        main.post {
            Approvals.requestGate(app, adm.approvalId, adm.wording, hint, crop, offer?.let { grantText(it, adm) }, adm.level)
        }
    }

    private fun num(v: Any?): Int = (v as? Number)?.toInt() ?: v?.toString()?.toDoubleOrNull()?.toInt() ?: -1

    /** What "Always allow" would let anyone driving the phone do (D20, S4). Not offered for browser autofill. */
    private fun grantText(offer: GrantOffer, adm: Admission): Approvals.Always {
        val days = offer.durationMs / 86_400_000.0
        val span = if (days >= 2) "${days.toInt()} days" else "24 hours"
        // The owner reads the app's name ("Example Shop"), not its package (seen as com.example.shop on the emulator).
        val pkg = offer.binding.substringBefore('>')
        val app = runCatching { GateScreens.labels(this.app.packageManager, listOf(pkg))[pkg] }.getOrNull() ?: pkg
        val text = "for $span, OpenMob (or a compromised server) could " + when (offer.kind) {
            "send" -> "send any $app message as you without asking"
            "post" -> "post anything on $app as you without asking"
            "delete" -> "delete anything in $app without asking"
            "pay" -> "pay for anything in $app without asking"
            "share" -> "share anything from $app to ${offer.binding.substringAfter('>')} without asking"
            "settings" -> "change this Settings page without asking"
            "permission" -> offer.binding.split('|').let {
                "allow ${it.getOrNull(1)?.replace('_', ' ')} (${it.getOrNull(2)}) for this app in permission dialogs without asking"
            }
            else -> "fill your saved passwords and cards into $app without asking"
        }
        val target = if (offer.kind == "permission") offer.binding.substringBefore('|') else adm.pkg
        return Approvals.Always(target, offer.kind, days, text, offer.confirmTwice)
    }

    fun approvalMessage(id: String, grant: Boolean): String? = gk().approvalMessage(id, grant)

    /** The owner passed the screen lock: [signature] was made by the auth-bound key over [message]. */
    fun approve(id: String, message: String, signature: String): Boolean = gk().approve(id, message, signature).also {
        if (it) { save(); dev.openmob.companion.ScreenKeeper.ownerActive() }
    }

    fun deny(id: String) = gk().deny(id)

    /** The prompt is on the owner's screen (ApprovalActivity): it counts toward the prompt limit now. */
    fun markShown(id: String) { gate?.markShown(id) }
    fun cancel(id: String): Boolean = id.startsWith("gk") && gate?.cancel(id) == true

    /** Is one of the phone's own prompts open (the indicator turns amber)? */
    fun promptPending(): Boolean = gate?.promptPending() == true

    /** The owner tapped the indicator's Stop: every open prompt closes unanswered, and the server hears it
     *  (gate_event expired) before the connection closes. */
    fun withdrawPrompts() { gate?.pendingIds()?.forEach { cancel(it) } }

    private fun onEvent(id: String, state: String) {
        sink?.invoke(JSONObject().put("type", "gate_event").put("approval_id", id).put("state", state))
        dev.openmob.companion.ControlIndicator.refresh() // a prompt opened or closed: amber or not
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

    fun resume() { gate?.resume(); dev.openmob.companion.ControlIndicator.refresh() }
    fun everydayOff() { gate?.let { if (it.everyday) { it.everydayOff(); save() } } }

    /** A permission-dialog default from the server: taken only if it tightens (loosening is signed on the phone). */
    fun tightenPermission(perm: String, value: String) { if (gate?.tightenPermission(perm, value) == true) save() }
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
