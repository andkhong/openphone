package dev.openmob.companion.gate

import java.util.UUID

/*
 * The phone's policy engine (docs/design/phone-gatekeeper.md, Phase 1). ConnectionService calls [Gatekeeper.admit]
 * first for every call (D1); PhoneActions calls [Gatekeeper.reverify] right before each side effect, under the
 * gesture lock (D8). A server can at most *request*: hard denies refuse, risky actions need the owner's
 * screen lock on a prompt the phone words itself (D5), and the mode is the owner's signed ceiling (D16).
 *
 * Plain Kotlin, no Android: the JVM tests run it on the shared fixtures (tests/fixtures/gatekeeper), which the
 * Python reference implementation (tests/gatekeeper_ref.py) also runs. Keep the two in step.
 * Every expiry is on [MonoClock] (D22): the wall clock is never read here.
 */

/** Elapsed time since boot (SystemClock.elapsedRealtime on the phone). */
fun interface MonoClock { fun elapsed(): Long }

/** Checks a signature by the owner's auth-bound approval key (ApprovalKeys on the phone). */
fun interface GateVerifier { fun verify(message: String, signature: String): Boolean }

class Risk(
    val kind: String, val reason: String, val money: String? = null, val always: Boolean = false,
    var grant: String? = null, var wording: String = "",
)

class Judgement(
    var refuse: Triple<String, String, String>? = null, var pkg: String = "", var fp: String = "",
    var strictFp: String = "", var risk: Risk? = null, var changing: Boolean = false, var payment: Boolean = false,
    var app: Boolean = false,
)

class GrantOffer(val kind: String, val binding: String, val durationMs: Long, val confirmTwice: Boolean) {
    fun record(): Map<String, Any?> = mapOf("type" to "grant", "kind" to kind, "binding" to binding, "duration_ms" to durationMs)
}

class Admission(
    val decision: String, val code: String = "", val rule: String = "", val message: String = "",
    val kind: String = "", val pkg: String = "", val approvalId: String = "", val wording: String = "",
    val grantOffer: GrantOffer? = null, val binding: String = "", val strict: Boolean = false,
    val permit: String? = null, val method: String = "", val params: Map<String, Any?> = emptyMap(),
    val money: String? = null,
) {
    val allowed get() = decision == "allow"
}

class PolicyProof(val message: String, val signature: String, val extra: Map<String, String> = emptyMap())

/** A signed owner-policy record as stored on the phone. `issued`: monotonic ms; `remaining`: last saved. */
class PolicyEntry(val record: Map<String, Any?>, val proof: PolicyProof, var issued: Long = 0, var remaining: Long? = null)

class Gatekeeper(
    val rules: GateRules,
    private val clock: MonoClock,
    private val verifier: GateVerifier,
    val device: String,
    /** Extra proof formats (a server-relayed permission change approved with the screen lock, see GateHost). */
    private val extraProof: ((canon: String, record: Map<String, Any?>, proof: PolicyProof) -> Boolean)? = null,
) {
    private class Grant(val kind: String, val binding: String, val durationMs: Long, var issued: Long, val canon: String)
    class Pending(val id: String, val binding: String, val created: Long, val risk: Risk, val pkg: String,
                  val task: String, val offer: GrantOffer?, val method: String, val params: Map<String, Any?>) {
        var cropHash = ""
    }
    private class Never(var issued: Long, val durationMs: Long, val allowMoney: Boolean)
    private class Followup(val pkg: String, val task: String, val expires: Long)
    private class Credit(val kind: String, val expires: Long)

    private val lock = Any()
    private val permits = HashMap<String, Long>()
    private val pending = LinkedHashMap<String, Pending>()
    private var followup: Followup? = null
    private val credits = mutableListOf<Credit>()
    private val grants = mutableListOf<Grant>()
    var ceiling = "auto"
        private set
    private var never: Never? = null
    private var tokens = rules.limit("act_burst").toDouble()
    private var lastRefill = clock.elapsed()
    private val asks = mutableListOf<Long>()
    private val denials = mutableListOf<Long>()
    var suspended = false
        private set
    var lastApp = ""
        private set
    var hwm = clock.elapsed()
        private set
    /** Signed records to persist (and re-verify on load). */
    private val entries = mutableListOf<PolicyEntry>()
    /** gate_event states for the server: pending | approved | denied | expired. */
    var onEvent: ((approvalId: String, state: String) -> Unit)? = null

    fun now(): Long { val e = clock.elapsed(); if (e > hwm) hwm = e; return e }

    private fun lim(key: String) = rules.limit(key)

    private fun expire(now: Long) {
        val gone = pending.values.filter { now - it.created >= lim("pending_ms") }.map { it.id }
        gone.forEach { pending.remove(it); onEvent?.invoke(it, "expired") }
        permits.entries.removeAll { it.value <= now }
        if ((followup?.expires ?: Long.MAX_VALUE) <= now) followup = null
        credits.removeAll { it.expires <= now }
        val expired = grants.filter { it.issued + it.durationMs <= now }.map { it.canon }.toSet()
        grants.removeAll { it.canon in expired }
        entries.removeAll { canon(it.record) in expired }
        never?.let { if (it.issued + it.durationMs <= now) { never = null; entries.removeAll { e -> e.record["type"] == "never" } } }
    }

    // --- signed owner policy -------------------------------------------------------------------

    /** Verify and apply a signed record. A bad signature is dropped: the built-ins apply (fail closed). */
    fun addPolicy(record: Map<String, Any?>, proof: PolicyProof): Boolean = synchronized(lock) {
        val c = canon(record)
        if (!verifyProof(c, record, proof)) return false
        val entry = PolicyEntry(record, proof, issued = now())
        if (!apply(record, c, entry.issued)) return false
        entries.removeAll { canon(it.record) == c || (record["type"] in setOf("ceiling", "never") && it.record["type"] == record["type"]) }
        entries.add(entry)
        true
    }

    private fun verifyProof(c: String, record: Map<String, Any?>, proof: PolicyProof): Boolean {
        val m = proof.message
        val shaped = m == "gk1|policy|$c" || (m.startsWith("gk1|$device|") && m.endsWith("|grant:$c"))
        if (shaped) return verifier.verify(m, proof.signature)
        return extraProof?.invoke(c, record, proof) == true
    }

    private fun apply(record: Map<String, Any?>, c: String, issued: Long): Boolean {
        when (record["type"]) {
            "ceiling" -> {
                val mode = record["mode"] as? String ?: return false
                if (mode !in setOf("ask", "plan", "auto", "trusted")) return false
                ceiling = mode
                return true
            }
            "never" -> {
                val d = minOf(record.long("duration_ms"), lim("never_max_ms"))
                if (d <= 0) return false
                never = Never(issued, d, record["allow_money"] == true)
                return true
            }
            "grant" -> {
                val kind = record["kind"] as? String ?: return false
                val binding = record["binding"]?.toString().orEmpty()
                if (kind !in setOf("send", "post", "delete", "pay", "share", "settings", "fill") || binding.isEmpty()) return false
                val cap = if (kind == "pay") lim("pay_grant_max_ms") else lim("grant_max_ms")
                val d = minOf(record.long("duration_ms"), cap)
                if (d <= 0) return false
                grants.removeAll { it.canon == c }
                grants.add(Grant(kind, binding, d, issued, c))
                return true
            }
        }
        return false
    }

    /** What to save (app-private storage): each signed record with its monotonic issue time and remaining time,
     *  plus the high-water mark of the monotonic clock. Saved every minute and at shutdown (D22). */
    fun persist(): Pair<List<PolicyEntry>, Long> = synchronized(lock) {
        val now = now()
        expire(now)
        for (e in entries) {
            val c = canon(e.record)
            e.issued = when (e.record["type"]) {
                "grant" -> grants.firstOrNull { it.canon == c }?.issued ?: e.issued
                "never" -> never?.issued ?: e.issued
                else -> e.issued
            }
            e.remaining = when (e.record["type"]) {
                "grant" -> grants.firstOrNull { it.canon == c }?.let { it.issued + it.durationMs - now }
                "never" -> never?.let { it.issued + it.durationMs - now }
                else -> null
            }
        }
        entries.map { PolicyEntry(it.record, it.proof, it.issued, it.remaining) } to hwm
    }

    /** Load saved records (after a process start or a reboot). Each is verified again. After a reboot the
     *  monotonic clock restarted, so a timed record counts down from its saved remaining time; one without a
     *  readable remaining time expires (fail closed). Setting the date changes nothing here. */
    fun restore(saved: List<PolicyEntry>, savedHwm: Long): Int = synchronized(lock) {
        val now = now()
        val rebooted = now < savedHwm
        if (!rebooted && savedHwm > hwm) hwm = savedHwm
        var loaded = 0
        for (e in saved) {
            val c = canon(e.record)
            if (!verifyProof(c, e.record, e.proof)) continue
            val timed = e.record["type"] in setOf("grant", "never")
            val issued = if (!timed || !rebooted) e.issued else {
                val rem = e.remaining ?: continue
                if (rem <= 0) continue
                val duration = if (e.record["type"] == "never") minOf(e.record.long("duration_ms"), lim("never_max_ms"))
                else minOf(e.record.long("duration_ms"), if (e.record["kind"] == "pay") lim("pay_grant_max_ms") else lim("grant_max_ms"))
                now - (duration - rem)
            }
            if (apply(e.record, c, issued)) {
                entries.add(PolicyEntry(e.record, e.proof, issued, e.remaining))
                loaded++
            }
        }
        expire(now)
        loaded
    }

    /** The effective mode: min(requested, ceiling) by rank (D16). A claim can only tighten. */
    fun mode(requested: String?): String = synchronized(lock) {
        val c = if (never != null) "never" else ceiling
        val r = if (requested != null && requested in RANK) requested else c
        val m = if (RANK.getValue(r) <= RANK.getValue(c)) r else c
        if (m == "plan") "auto" else m
    }

    val neverActive: Boolean get() = synchronized(lock) { expire(now()); never != null }
    val neverMoney: Boolean get() = synchronized(lock) { never?.allowMoney == true }
    /** Monotonic ms left in the `never` window (0: off), for the phone's own banner. */
    val neverRemaining: Long get() = synchronized(lock) { never?.let { maxOf(0, it.issued + it.durationMs - now()) } ?: 0 }

    fun grantsSummary(): List<String> = synchronized(lock) {
        val now = now()
        expire(now)
        grants.map { "${it.kind} · ${it.binding} · ${(it.issued + it.durationMs - now) / 3_600_000} h left" }
    }

    fun revokeGrant(index: Int) = synchronized(lock) {
        val g = grants.getOrNull(index) ?: return@synchronized
        grants.remove(g)
        entries.removeAll { canon(it.record) == g.canon }
    }

    // --- the owner's own instruction (D6) -------------------------------------------------------

    fun noteInstruction(text: String) = synchronized(lock) {
        val toks = GateText.tokens(text)
        val now = now()
        for (kind in listOf("send", "post", "delete")) {
            if (GateText.anyPhrase(toks, rules.w.getValue(kind)) != null) credits.add(Credit(kind, now + lim("requested_ms")))
        }
    }

    // --- kill switch ------------------------------------------------------------------------------

    fun resume() = synchronized(lock) { suspended = false; denials.clear() }

    /** The `never` window ends now (a tightening: no signature needed). */
    fun revokeNeverOnly() = synchronized(lock) { never = null; entries.removeAll { it.record["type"] == "never" } }

    /** Stop / Disconnect: every permit, pending prompt, purchase flow and requested credit goes, and `never`
     *  drops back to the ceiling at once. "Always allow" grants stay (they're listed and revocable in the app). */
    fun revokeAll() = synchronized(lock) {
        pending.keys.toList().forEach { onEvent?.invoke(it, "expired") }
        permits.clear(); pending.clear(); followup = null; credits.clear()
        never = null
        entries.removeAll { it.record["type"] == "never" }
        suspended = false
    }

    val promptOpen: Boolean get() = synchronized(lock) { pending.isNotEmpty() }
    fun pending(id: String): Pending? = synchronized(lock) { pending[id] }

    // --- admit ------------------------------------------------------------------------------------

    fun admit(method: String, params: Map<String, Any?>, screen: GScreen): Admission = synchronized(lock) {
        val now = now()
        expire(now)
        if (method in rules.meta) return Admission("allow", method = method, params = params)
        if (method in rules.read || method.startsWith("stream_")) {
            if ((method == "screenshot" || method == "ui_elements") && screen.windows.any { rules.blocked(it.pkg) }) {
                return Admission("redact", rule = "builtin:sensitive_apps", method = method, params = params)
            }
            return Admission("allow", method = method, params = params)
        }
        if (method !in rules.act) return refuse("gate_unknown_method", "builtin:methods", "Unknown method $method.", method, params)
        if (pending.isNotEmpty()) return refuse("gate_prompt_open", "builtin:prompt_open",
            "Waiting for the owner to answer a prompt on the phone.", method, params)
        if (method != "wake_unlock" && screen.keyguardLocked && screen.keyguardSecure) {
            return refuse("phone_locked", "builtin:keyguard", "The phone is locked. Unlock it to continue.", method, params)
        }
        val j = judge(method, params, screen)
        j.refuse?.let { return refuse(it.first, it.second, it.third, method, params, j.pkg) }
        if (suspended) return refuse("gate_rate_limited", "builtin:suspended",
            "Paused on the phone after repeated denials. The owner can resume.", method, params)
        if (!takeToken(now)) return refuse("gate_rate_limited", "builtin:act_rate", "Too many actions. Slow down.", method, params)
        if (j.app && j.pkg.isNotEmpty() && followup != null && j.pkg != followup!!.pkg) followup = null
        if (j.app && j.pkg.isNotEmpty()) lastApp = j.pkg
        val mode = mode(params["mode"] as? String)
        var risk = j.risk
        val hint = params["hint"]?.toString()?.trim().orEmpty()
        if (risk == null && hint.isNotEmpty() && mode != "never") risk = Risk("other", "server_hint", wording = wording(method, params, j, screen))
        if (mode == "ask" && j.changing && risk == null) risk = Risk("step", "mode:ask", wording = wording(method, params, j, screen))
        val bind = GateScreen.binding(method, params, j.strictFp)
        if (risk == null) return Admission("allow", strict = j.payment, binding = bind, method = method, params = params, pkg = j.pkg)
        if (mode == "never") {
            if (risk.money != null && never?.allowMoney != true) return refuse("gate_denied", "never:money",
                "Purchases are off while agents act without asking.", method, params, j.pkg)
            if (risk.always) return refuse("gate_denied", "never:${risk.reason}",
                "Nobody can be asked while agents act without asking, so this is refused.", method, params, j.pkg)
            return Admission("allow", strict = true, binding = bind, kind = risk.kind, method = method, params = params, pkg = j.pkg)
        }
        if (bind in permits) return Admission("allow", strict = true, binding = bind, permit = bind, kind = risk.kind,
            money = risk.money, method = method, params = params, pkg = j.pkg)
        if (mode != "ask") covered(risk, j.pkg, params)?.let {
            return Admission("allow", strict = true, binding = bind, kind = risk.kind, rule = it, money = risk.money,
                method = method, params = params, pkg = j.pkg)
        }
        asks.removeAll { now - it >= lim("ask_window_ms") }
        if (asks.size >= lim("asks_per_window")) return refuse("gate_rate_limited", "builtin:prompt_rate",
            "Too many approval prompts. Try later.", method, params, j.pkg)
        asks.add(now)
        val id = UUID.randomUUID().toString().replace("-", "")
        val offer = offer(risk, mode)
        pending[id] = Pending(id, bind, now, risk, j.pkg, params["task"]?.toString().orEmpty(), offer, method, params)
        onEvent?.invoke(id, "pending")
        Admission("ask", code = "gate_needs_approval", rule = "risk:${risk.reason}", kind = risk.kind, approvalId = id,
            wording = risk.wording, grantOffer = offer, binding = bind, money = risk.money,
            message = "Waiting for the owner's approval on the phone.", method = method, params = params, pkg = j.pkg)
    }

    private fun refuse(code: String, rule: String, message: String, method: String, params: Map<String, Any?>, pkg: String = "") =
        Admission("refuse", code = code, rule = rule, message = message, method = method, params = params, pkg = pkg)

    private fun takeToken(now: Long): Boolean {
        tokens = minOf(lim("act_burst").toDouble(), tokens + maxOf(0L, now - lastRefill) * lim("act_per_second") / 1000.0)
        lastRefill = now
        if (tokens < 1) return false
        tokens -= 1
        return true
    }

    private fun covered(risk: Risk, pkg: String, params: Map<String, Any?>): String? {
        val now = now()
        val f = followup
        if (risk.money == "terminal" && f != null && f.pkg == pkg && f.task == params["task"]?.toString().orEmpty()) {
            followup = null
            return "permit:purchase_flow"
        }
        if (risk.kind in setOf("send", "post", "delete") && risk.money == null) {
            credits.firstOrNull { it.kind == risk.kind }?.let { credits.remove(it); return "requested" }
        }
        val g = risk.grant
        if (g != null && grants.any { it.kind == risk.kind && it.binding == g && it.issued + it.durationMs > now }) return "grant"
        return null
    }

    private fun offer(risk: Risk, mode: String): GrantOffer? {
        val g = risk.grant ?: return null
        if (mode == "ask") return null
        val ms = if (risk.kind == "pay") lim("pay_grant_default_ms") else lim("grant_default_ms")
        return GrantOffer(risk.kind, g, ms, risk.kind == "pay")
    }

    // --- approvals ---------------------------------------------------------------------------------

    /** What the owner's screen-lock signature covers (D5): this device, this prompt, this exact action. */
    fun approvalMessage(id: String, grant: Boolean = false): String? = synchronized(lock) {
        val p = pending[id] ?: return null
        var m = "gk1|$device|$id|${p.binding}"
        if (p.cropHash.isNotEmpty()) m += "|crop:${p.cropHash}"
        if (grant && p.offer != null) m += "|grant:" + canon(p.offer.record())
        m
    }

    /** The SHA-256 of the crop the prompt shows (the owner checks item and price in it): the signature covers it. */
    fun setCrop(id: String, hash: String) = synchronized(lock) { pending[id]?.cropHash = hash }

    /** The owner passed the screen lock on the phone's own prompt: mint a one-shot permit for exactly that
     *  action, and, if they ticked it, the "Always allow" grant. */
    fun approve(id: String, message: String, signature: String): Boolean = synchronized(lock) {
        val now = now()
        expire(now)
        val p = pending[id] ?: return false
        val plain = approvalMessage(id, false)
        var grant: GrantOffer? = null
        if (message != plain) {
            if (p.offer == null || message != approvalMessage(id, true)) return false
            grant = p.offer
        }
        if (!verifier.verify(message, signature)) return false
        pending.remove(id)
        permits[p.binding] = now + lim("permit_ms")
        if (p.risk.money == "start") followup = Followup(p.pkg, p.task, now + lim("purchase_followup_ms"))
        if (grant != null) {
            val record = grant.record()
            val c = canon(record)
            if (apply(record, c, now)) {
                entries.removeAll { canon(it.record) == c }
                entries.add(PolicyEntry(record, PolicyProof(message, signature), now))
            }
        }
        onEvent?.invoke(id, "approved")
        true
    }

    fun cancel(id: String): Boolean = synchronized(lock) {
        if (pending.remove(id) == null) return false
        onEvent?.invoke(id, "expired")
        true
    }

    fun deny(id: String): Boolean = synchronized(lock) {
        val now = now()
        if (pending.remove(id) == null) return false
        denials.removeAll { now - it >= lim("denial_window_ms") }
        denials.add(now)
        if (denials.size >= lim("denials_to_suspend")) suspended = true
        onEvent?.invoke(id, "denied")
        true
    }

    // --- reverify: right before the side effect, under the gesture lock (D8) ------------------------

    fun reverify(adm: Admission, screen: GScreen): Admission = synchronized(lock) {
        if (!adm.allowed || adm.method !in rules.act) return adm
        val j = judge(adm.method, adm.params, screen)
        j.refuse?.let {
            adm.permit?.let(permits::remove)
            return refuse(it.first, it.second, it.third, adm.method, adm.params, j.pkg)
        }
        if (adm.strict) {
            if (GateScreen.binding(adm.method, adm.params, j.strictFp) != adm.binding) {
                adm.permit?.let(permits::remove)
                return refuse("gate_screen_changed", "builtin:toctou", "The screen changed before the action ran. Look again.",
                    adm.method, adm.params, j.pkg)
            }
            if (screen.quietMs < lim("quiet_ms")) return refuse("gate_screen_unstable", "builtin:toctou",
                "The screen is still changing. Try again in a moment.", adm.method, adm.params, j.pkg)
        } else if (j.risk != null || j.payment) {
            return refuse("gate_screen_changed", "builtin:toctou", "The screen changed before the action ran. Look again.",
                adm.method, adm.params, j.pkg)
        }
        if (adm.permit != null && permits.remove(adm.permit) == null) {
            return refuse("gate_screen_changed", "builtin:permit", "That approval was already used.", adm.method, adm.params, j.pkg)
        }
        adm
    }

    // --- the judge -------------------------------------------------------------------------------------

    private fun label(screen: GScreen, pkg: String) = screen.labels[pkg]?.takeIf { it.isNotEmpty() } ?: pkg

    private fun wording(method: String, params: Map<String, Any?>, j: Judgement, screen: GScreen): String {
        val app = if (j.pkg.isNotEmpty()) label(screen, j.pkg) else "this phone"
        return when (method) {
            "type_text" -> "Allow typing into a field in $app?"
            "key" -> "Allow pressing Enter in $app?"
            "launch_app" -> "Allow opening $app?"
            "open_url" -> "Allow opening ${params["url"]?.toString().orEmpty().take(80)} in $app?"
            else -> "Allow this action in $app?"
        }
    }

    private fun num(params: Map<String, Any?>, key: String, default: Double): Double = when (val v = params[key]) {
        is Number -> v.toDouble()
        is String -> v.toDoubleOrNull() ?: default
        else -> default
    }

    fun judge(method: String, params: Map<String, Any?>, screen: GScreen): Judgement = when (method) {
        "tap", "long_press" -> judgePoint(num(params, "x", -1.0), num(params, "y", -1.0), screen)
        "swipe" -> {
            val x1 = num(params, "x1", -1.0); val y1 = num(params, "y1", -1.0)
            val x2 = num(params, "x2", -1.0); val y2 = num(params, "y2", -1.0)
            val duration = num(params, "duration_ms", 300.0)
            val dist = Math.hypot(x2 - x1, y2 - y1)
            if (dist <= 2 * rules.swipe.long("slop_px") || duration < rules.swipe.long("tap_ms")) judgePoint(x1, y1, screen)
            else judgeSwipe(x1, y1, x2, y2, screen)
        }
        "type_text" -> judgeTyping(params["text"]?.toString().orEmpty(), screen)
        "key" -> judgeKey(params["name"]?.toString().orEmpty().uppercase(), screen)
        "launch_app" -> judgeLaunch(params["package"]?.toString().orEmpty())
        "open_url" -> judgeUrl(params["url"]?.toString().orEmpty(), screen)
        else -> Judgement() // wake_unlock: wakes the screen, never touches a secure lock
    }

    private fun surfaceRefusal(w: GWindow): Triple<String, String, String>? {
        if (w.pkg in rules.own) return Triple("gate_denied_page", "builtin:own_window", "That's OpenMob's own screen; agents can't touch it.")
        if (w.pkg == rules.surfStr("systemui")) {
            val title = rules.rx(rules.surfStr("prompt_titles"))
            val ids = rules.rx(rules.surfStr("prompt_ids"))
            if (title.containsMatchIn(w.title) || walk(w.root).any { ids.containsMatchIn(it.id) }) {
                return Triple("gate_denied_page", "builtin:prompt_surface",
                    "That's the lock screen or a system approval prompt; agents can't touch it.")
            }
        }
        if (w.pkg in rules.installers) return Triple("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
        if (rules.blocked(w.pkg)) return Triple("gate_blocked_app", "builtin:sensitive_apps",
            "Blocked on the phone: ${w.pkg} is a sensitive app. Don't open it another way; call done.")
        return null
    }

    private fun appWindow(w: GWindow): Boolean = w.type == "application" && w.pkg != rules.surfStr("systemui") &&
        !isSettings(w) && w.pkg !in rules.surfList("share_packages") && w.pkg !in rules.surfList("credential_packages")

    private fun isSettings(w: GWindow): Boolean {
        if (rules.settingsPkg.containsMatchIn(w.pkg)) return true
        return w.pkg == rules.sStr("gms_package") && rules.rx(rules.sStr("gms_settings_titles")).containsMatchIn(w.title)
    }

    private fun has(toks: List<String>, phrases: List<String>) = GateText.anyPhrase(toks, phrases) != null

    /** ("refuse"|"blocked"|"ask"|"allow"|"none", rule or page fingerprint). */
    private fun settingsTier(w: GWindow, t: GTarget?, screen: GScreen, label: List<String>): Pair<String, String> {
        val title = GateScreen.pageTitle(w)
        val activity = if (w.type == "application") screen.activity else ""
        if (has(title, rules.sList("refuse")) || has(label, rules.sList("refuse")) ||
            (activity.isNotEmpty() && rules.rx(rules.sStr("refuse_activities")).containsMatchIn(activity))) {
            return "refuse" to "builtin:settings_security"
        }
        val page = listOf(w.pkg, activity, title.joinToString(" ")).joinToString("/")
        val screenToks = GateText.tokens(walk(w.root).joinToString(" ") { "${it.text} ${it.desc}" })
        if (has(title, rules.sList("app_info_markers")) || (GateText.hasPhrase(screenToks, "force stop") &&
                (GateText.hasPhrase(screenToks, "uninstall") || GateText.hasPhrase(screenToks, "disable")))) {
            val blockedLabels = screen.labels.filter { rules.blocked(it.key) }.map { GateText.tokens(it.value) }
            val texts = walk(w.root).filter { it.text.isNotEmpty() }.map { GateText.tokens(it.text) }
            if (blockedLabels.any { it.isNotEmpty() && it in texts }) return "blocked" to "builtin:app_info_blocked"
            if (has(label, rules.sList("app_info_ask"))) return "ask" to page
            if (has(label, rules.sList("app_info_allowed"))) return "allow" to ""
            return "none" to ""
        }
        if (has(title, rules.sList("ask_pages")) || has(label, rules.sList("ask_pages"))) {
            if (label.isNotEmpty() && GateText.anyPhrase(label, rules.w.getValue("navigation")) == label.joinToString(" ")) return "allow" to ""
            return "ask" to page
        }
        if (rules.rx(rules.sStr("permission_packages")).containsMatchIn(w.pkg) || GateText.hasPhrase(title, "permissions") ||
            GateText.hasPhrase(title, "permission")) {
            val session = if (lastApp.isNotEmpty()) GateText.tokens(screen.labels[lastApp].orEmpty()) else emptyList()
            if (session.isNotEmpty() && GateText.hasPhrase(screenToks, session.joinToString(" "))) return "allow" to ""
            return if (changingTap(t, label)) "ask" to page else "none" to ""
        }
        if (has(title, rules.sList("everyday")) || has(label, rules.sList("everyday"))) return "allow" to ""
        return if (changingTap(t, label)) "ask" to page else "none" to ""
    }

    private fun changingTap(t: GTarget?, label: List<String>): Boolean {
        if (t != null && listOfNotNull(t.pressed, t.hit).any { it.checked != null }) return true
        return has(label, rules.sList("change_words"))
    }

    private fun wordRisk(label: List<String>, ids: List<String>, pkg: String, payment: Boolean): Risk? {
        val words = GateText.stripPhrases(label, rules.w.getValue("pre_commit"))
        val idWords = mutableListOf<String>()
        val risky = rules.list("resource_id_risky")
        val pre = rules.list("resource_id_pre_commit")
        for (rid in ids) {
            val toks = GateText.idTokens(rid)
            for (w in risky) if (w !in pre && (GateText.hasPhrase(toks, w) || (' ' !in w && w in toks))) idWords.add(w)
        }
        val moneyCtx = payment || rules.moneyApp.containsMatchIn(pkg)
        val flow = GateText.anyPhrase(words, rules.w.getValue("flow_start")) ?: idWords.firstOrNull { it in setOf("buy", "purchase", "transfer") }
        if (flow != null) return Risk("pay", "words:$flow", money = "start", grant = pkg)
        val terminal = GateText.anyPhrase(words, rules.w.getValue("terminal"))
            ?: idWords.firstOrNull { it in setOf("pay", "place order", "placeorder", "confirm") }
        if (terminal != null) {
            if (terminal == "confirm" && !moneyCtx) return Risk("other", "words:confirm")
            return Risk("pay", "words:$terminal", money = "terminal", grant = pkg)
        }
        for (kind in listOf("send", "post", "delete")) {
            GateText.anyPhrase(words, rules.w.getValue(kind))?.let { return Risk(kind, "words:$it", grant = pkg) }
        }
        for (w in idWords) when (w) {
            "send" -> return Risk("send", "id:send", grant = pkg)
            "delete", "remove" -> return Risk("delete", "id:$w", grant = pkg)
            "submit" -> return Risk("other", "id:submit")
        }
        return null
    }

    private fun sliderAt(t: GTarget): Boolean {
        val rx = rules.rxKey("slider_class")
        return listOfNotNull(t.pressed, t.hit).any { rx.containsMatchIn(it.cls) }
    }

    private fun judgePoint(x: Double, y: Double, screen: GScreen): Judgement {
        val t = GateScreen.hitTest(rules, screen, x, y) ?: return Judgement(changing = true)
        val w = t.window
        val j = Judgement(pkg = w.pkg, fp = t.fp(false), strictFp = t.fp(true), changing = true, app = appWindow(w))
        surfaceRefusal(w)?.let { j.refuse = it; return j }
        val pressed = t.pressed
        if (pressed != null && (pressed.editable || "EditText" in pressed.cls)) j.changing = false
        val label = t.labelTokens()
        val ids = t.ids()
        j.payment = GateScreen.paymentContext(rules, screen)
        val shown = t.shown()
        val appLabel = label(screen, w.pkg)
        val source = lastApp.ifEmpty { w.pkg }
        val inBrowser = rules.isCat("browser", source) || t.inWebView()
        fun wording() = if (shown.isNotEmpty()) "Allow tapping “$shown” in $appLabel?" else "Allow tapping an unlabeled spot in $appLabel?"

        var tier = "none" to ""
        if (isSettings(w)) {
            tier = settingsTier(w, t, screen, label)
            if (tier.first == "refuse") { j.refuse = Triple("gate_denied_page", tier.second, "This Settings page is security-critical; agents can't change it."); return j }
            if (tier.first == "blocked") { j.refuse = Triple("gate_blocked_app", tier.second, "That's a blocked app's settings; agents can't touch it."); return j }
        }
        val surface = w.type == "input_method" || w.pkg in rules.surfList("credential_packages") ||
            (screen.autofillService.isNotEmpty() && w.pkg == screen.autofillService) ||
            (w.pkg == rules.sStr("gms_package") && rules.rx(rules.surfStr("credential_titles")).containsMatchIn(w.title))
        if (surface || has(label, rules.w.getValue("credential"))) {
            val grant = if (inBrowser || rules.blocked(screen.autofillService)) null else source
            j.risk = Risk("fill", "autofill", always = true, grant = grant,
                wording = "Fill your saved card or password into ${label(screen, source)}?")
            return j
        }
        if (w.pkg in rules.surfList("share_packages") &&
            (rules.rx(rules.surfStr("share_titles")).containsMatchIn(w.title) || w.pkg != "android")) {
            j.risk = Risk("share", "share_sheet", grant = "$source>${label.joinToString(" ")}",
                wording = "Share to “${shown.ifEmpty { "this target" }}”?")
            return j
        }
        if (w.pkg == rules.surfStr("systemui") && pressed != null && pressed !== w.root &&
            (rules.rx(rules.surfStr("notification_action_ids")).containsMatchIn(pressed.id) || "Button" in pressed.cls)) {
            j.risk = Risk("other", "notification_action", wording = wording())
            return j
        }
        var risk = wordRisk(label, ids, w.pkg, j.payment)
        if (risk == null && (sliderAt(t) || has(label, rules.w.getValue("slide")))) {
            risk = Risk(if (j.payment) "pay" else "other", "slider", money = if (j.payment) "terminal" else null,
                grant = if (j.payment) w.pkg else null)
        }
        val moneyApp = rules.moneyApp.containsMatchIn(w.pkg) || j.payment
        if (risk == null && moneyApp && !t.labeled()) {
            risk = Risk(if (j.payment) "pay" else "other", "unlabeled", money = if (j.payment) "terminal" else null)
        }
        if (risk == null && moneyApp && j.changing && !GateScreen.languageCovered(rules, screen) &&
            !(label.isNotEmpty() && has(label, rules.w.getValue("navigation")))) {
            risk = Risk("other", "language")
        }
        if (risk == null && tier.first == "ask") risk = Risk("settings", "settings_page", always = true, grant = tier.second)
        if (risk != null) {
            if (risk.wording.isEmpty()) risk.wording = wording()
            if (risk.grant != null && risk.kind == "pay" && inBrowser) risk.grant = w.pkg
        }
        j.risk = risk
        return j
    }

    private fun judgeSwipe(x1: Double, y1: Double, x2: Double, y2: Double, screen: GScreen): Judgement {
        val start = GateScreen.hitTest(rules, screen, x1, y1)
        val end = GateScreen.hitTest(rules, screen, x2, y2)
        val ends = listOf(start, end)
        val j = Judgement(pkg = start?.window?.pkg.orEmpty(), changing = false,
            fp = ends.joinToString("#") { it?.fp(false).orEmpty() }, strictFp = ends.joinToString("#") { it?.fp(true).orEmpty() },
            app = start != null && appWindow(start.window))
        for (t in ends) if (t != null) surfaceRefusal(t.window)?.let { j.refuse = it; return j }
        j.payment = GateScreen.paymentContext(rules, screen)
        for (t in ends) {
            if (t == null) continue
            val label = t.labelTokens()
            val slider = sliderAt(t) || has(label, rules.w.getValue("slide"))
            var risk = wordRisk(label, t.ids(), t.window.pkg, j.payment)
            if (risk == null && slider) {
                val money = j.payment || has(GateText.stripPhrases(label, rules.w.getValue("pre_commit")),
                    rules.w.getValue("flow_start") + rules.w.getValue("terminal"))
                risk = Risk(if (money) "pay" else "other", "slider", money = if (money) "terminal" else null,
                    grant = if (money) t.window.pkg else null)
            }
            if (risk != null) {
                risk.wording = "Allow sliding “${t.shown()}” in ${label(screen, t.window.pkg)}?"
                j.risk = risk
                j.changing = true
                return j
            }
        }
        return j
    }

    private fun judgeTyping(text: String, screen: GScreen): Judgement {
        val scheme = GateText.urlScheme(text)
        if (scheme != null && scheme in rules.schemesTypedRefused) {
            return Judgement(changing = true, refuse = Triple("gate_denied", "builtin:typed_scheme", "$scheme: text is never typed by agents."))
        }
        val f = GateScreen.focusedField(screen) ?: return Judgement(changing = true)
        val w = f.window
        val node = f.node
        val fp = "${w.id}|${w.pkg}|${node.id}/${node.hint}/${node.cls}/${if (node.password) 1 else 0}"
        val j = Judgement(pkg = w.pkg, fp = fp, strictFp = fp + "/" + node.bounds.joinToString(","), changing = true, app = appWindow(w))
        surfaceRefusal(w)?.let { j.refuse = it; return j }
        if (node.password) {
            j.refuse = Triple("gate_password_field", "builtin:password_field",
                "That's a password field. Agents never type passwords; ask the owner to type it.")
            return j
        }
        if (isSettings(w) && settingsTier(w, null, screen, emptyList()).first == "refuse") {
            j.refuse = Triple("gate_denied_page", "builtin:settings_security", "This Settings page is security-critical; agents can't change it.")
            return j
        }
        j.payment = GateScreen.paymentContext(rules, screen)
        val about = "${node.id} ${node.hint}"
        val app = label(screen, w.pkg)
        if (rules.rxKey("payment_field").containsMatchIn(about)) {
            j.risk = Risk("fill", "card_field", always = true, wording = "Allow typing into the card field in $app?")
        } else if (rules.rxKey("otp_field").containsMatchIn(about)) {
            j.risk = Risk("fill", "otp_field", always = true, wording = "Allow typing a code into $app?")
        }
        return j
    }

    private fun judgeKey(name: String, screen: GScreen): Judgement {
        if (name != "ENTER" && name != "DELETE") return Judgement(changing = false)
        val f = GateScreen.focusedField(screen) ?: return Judgement(changing = name == "ENTER")
        val w = f.window
        val node = f.node
        val fp = "${w.id}|${w.pkg}|${node.id}/${node.hint}/${node.cls}"
        val j = Judgement(pkg = w.pkg, fp = fp, strictFp = fp + "/" + node.bounds.joinToString(","), changing = name == "ENTER", app = appWindow(w))
        surfaceRefusal(w)?.let { j.refuse = it; return j }
        if (name == "DELETE") return j
        val browser = rules.isCat("browser", w.pkg)
        val webview = f.path.any { "WebView" in it.cls }
        if (browser && (rules.rxKey("url_bar").containsMatchIn(node.id) || GateText.looksLikeUrl(node.text))) {
            var url = node.text.trim()
            if (GateText.urlScheme(url) == null && GateText.looksLikeUrl(url)) url = "https://$url"
            val uj = judgeUrl(url, screen)
            uj.fp = fp + "|" + uj.fp
            uj.strictFp = j.strictFp + "|" + uj.strictFp
            uj.changing = true
            return uj
        }
        j.payment = GateScreen.paymentContext(rules, screen)
        val app = label(screen, w.pkg)
        val searchRx = rules.rxKey("search_field")
        val search = listOf(node.id, node.cls, node.hint, node.desc).any { searchRx.containsMatchIn(it) }
        if (j.payment) {
            j.risk = Risk("pay", "enter:payment", money = "terminal", grant = w.pkg, wording = "Allow pressing Enter on a checkout in $app?")
        } else if (listOf("messaging", "email", "social").any { rules.isCat(it, w.pkg) }) {
            j.risk = Risk("send", "enter:messaging", grant = w.pkg, wording = "Allow sending with Enter in $app?")
        } else if ((browser || webview) && !search) {
            j.risk = Risk("other", "enter:form", wording = "Allow submitting a form with Enter in $app?")
        }
        return j
    }

    private fun judgeLaunch(pkg: String): Judgement {
        val j = Judgement(pkg = pkg, fp = "launch|$pkg", strictFp = "launch|$pkg", changing = true, app = true)
        j.refuse = when {
            pkg in rules.own -> Triple("gate_blocked_app", "builtin:own_app", "OpenMob can't be opened by agents.")
            pkg in rules.installers -> Triple("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
            rules.blocked(pkg) -> Triple("gate_blocked_app", "builtin:sensitive_apps",
                "Blocked on the phone: $pkg is a sensitive app. Don't open it another way; call done.")
            else -> null
        }
        return j
    }

    private fun judgeUrl(url: String, screen: GScreen): Judgement {
        val scheme = GateText.urlScheme(url)
        val resolved = screen.resolve[url]
        val handler = resolved?.first.orEmpty()
        val candidates = resolved?.second?.takeIf { it.isNotEmpty() } ?: if (handler.isNotEmpty()) listOf(handler) else emptyList()
        val fp = "url|${scheme ?: "None"}|$handler"
        val j = Judgement(pkg = handler, fp = fp, strictFp = fp, changing = true, app = handler.isNotEmpty())
        if (scheme == null || scheme !in rules.schemesAllowed) {
            j.refuse = Triple("gate_denied", "builtin:url_scheme",
                "${scheme ?: "that"}: links aren't opened by agents (only https, http, tel, geo, mailto, sms and market).")
            return j
        }
        for (pkg in if (handler.isNotEmpty()) listOf(handler) else candidates) {
            if (pkg in rules.own || rules.blocked(pkg)) {
                j.refuse = Triple("gate_blocked_app", "builtin:sensitive_apps", "Blocked on the phone: that link opens $pkg, a sensitive app. Call done.")
                return j
            }
            if (pkg in rules.installers) {
                j.refuse = Triple("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
                return j
            }
        }
        if (scheme == "tel" && handler.isNotEmpty() && !rules.isCat("dialer", handler)) {
            j.refuse = Triple("gate_denied", "builtin:tel_handler", "tel: links only open the dialer.")
        }
        return j
    }

    companion object {
        val RANK = mapOf("ask" to 0, "plan" to 1, "auto" to 1, "trusted" to 2, "never" to 3)

        /** A record's canonical text: sorted key=value pairs (booleans as 1/0). */
        fun canon(record: Map<String, Any?>): String = record.filterValues { it != null }.toSortedMap().entries
            .joinToString(";") { (k, v) -> "$k=" + when (v) { is Boolean -> if (v) "1" else "0"; else -> v.toString() } }
    }
}
