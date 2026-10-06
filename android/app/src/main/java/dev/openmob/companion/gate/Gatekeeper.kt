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
    /** The target's words (a cart or checkout word ends a purchase flow). */
    var label: List<String> = emptyList(),
    /** strictFp plus the currency amounts a money commit is about (any change voids a permit). */
    var moneyFp: String = "",
    /** How a prompt names this action from what the phone sees (used when only the server's hint makes it ask). */
    var wording: String = "",
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
    /** The binding with every number blanked: tells a price change from a moved target. */
    val plain: String = "",
    /** An ask's confirmation level: tap (Allow/Deny, the tap key) | fingerprint (the screen lock). */
    val level: String = "",
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
    /** The tap key (not bound to the screen lock): only OpenMob's own Allow button signs with it. */
    private val tapVerifier: GateVerifier = GateVerifier { _, _ -> false },
) {
    private class Grant(val kind: String, val binding: String, val durationMs: Long, var issued: Long, val canon: String)
    class Pending(val id: String, val binding: String, val created: Long, val risk: Risk, val pkg: String,
                  val task: String, val offer: GrantOffer?, val method: String, val params: Map<String, Any?>) {
        var cropHash = ""
        /** Shown to the owner: only shown prompts count toward the prompt limit. */
        var counted = false
        /** tap: an on-phone Allow/Deny signed with the tap key; fingerprint: the screen lock. */
        var level = "fingerprint"
        /** A task prompt: the request text's digest. */
        var digest = ""
        /** A task prompt: the request text (the grant's scope comes from it). */
        var text = ""
        /** A just-in-time task prompt: approving it also permits the step that raised it. */
        var step = false
    }
    private class Never(var issued: Long, val durationMs: Long, val allowMoney: Boolean)
    private class Followup(val pkg: String, val task: String, val expires: Long, var acts: Int = 0)
    private class Credit(val kind: String, val expires: Long)

    private val lock = Any()
    private val permits = HashMap<String, Long>()
    private val pending = LinkedHashMap<String, Pending>()
    private var followup: Followup? = null
    /** binding -> (package, task): an approved flow-starting money tap; the follow-up starts when it runs. */
    private val startPermits = HashMap<String, Pair<String, String>>()
    private val credits = mutableListOf<Credit>()
    private class TaskGrant(val digest: String, val expires: Long, val words: List<String>, val kinds: Set<String>,
                            var used: Int = 0, var first: String = "", val apps: MutableSet<String> = mutableSetOf())
    /** When the last prompt closed (monotonic ms): no gesture for a moment after (B1). */
    private var promptClosed = Long.MIN_VALUE / 2
    /** task id -> the owner's approval of that task (task approval, by owner request). */
    private val taskGrants = HashMap<String, TaskGrant>()
    /** Requests typed or spoken on this phone (digest, expiry): a task for one needs no task prompt. */
    private val ownRequests = mutableListOf<Pair<String, Long>>()
    private val grants = mutableListOf<Grant>()
    var ceiling = "auto"
        private set
    /** The owner's permission-dialog defaults (signed records, or tightenings from anywhere). */
    private val permDefaults = HashMap<String, String>()
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
        gone.forEach { pending.remove(it); promptClosed = now; onEvent?.invoke(it, "expired") }
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
        // An "Always allow" ticked on a tap-level prompt: signed with the tap key (domain gk1|tap|).
        if (m.startsWith("gk1|tap|$device|") && m.endsWith("|grant:$c")) return tapVerifier.verify(m, proof.signature)

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
            "everyday" -> {
                everyday = record["on"] == true
                return true
            }
            "never" -> {
                val d = minOf(record.long("duration_ms"), lim("never_max_ms"))
                if (d <= 0) return false
                never = Never(issued, d, record["allow_money"] == true)
                return true
            }
            "permission_default" -> {
                val perm = record["perm"] as? String ?: return false
                val value = record["value"] as? String ?: return false
                if (value !in permValues(perm)) return false
                permDefaults[perm] = value
                return true
            }
            "grant" -> {
                val kind = record["kind"] as? String ?: return false
                val binding = record["binding"]?.toString().orEmpty()
                if (kind !in setOf("send", "post", "delete", "pay", "share", "settings", "fill", "permission") || binding.isEmpty()) return false
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
    private val permRules = rules.d["permission_dialog"].obj()

    /** The settings a permission takes, tightest first. */
    private fun permValues(perm: String): List<String> {
        val v = permRules["values"].obj()
        return (v[perm] ?: v["default"]).strings()
    }

    fun permSetting(perm: String): String = synchronized(lock) {
        val d = permRules["defaults"].obj()
        permDefaults[perm] ?: (d[perm] ?: d["default"]).toString()
    }

    /** A tightening (deny, ask, approximate…) is taken from anywhere, unsigned; loosening needs a signed record. */
    /** "Don't confirm everyday actions": signed by the owner; tap-level steps then run unconfirmed. */
    var everyday = false
        private set

    /** A tightening, from anywhere: everyday actions are confirmed again. */
    fun everydayOff() = synchronized(lock) { everyday = false; entries.removeAll { it.record["type"] == "everyday" } }

    /** Three confirmation levels (owner principle): a fingerprint only for money and dangerous actions (saved cards
     *  and passwords in a browser or web page, OTP or card fields, permission grants); a simple on-phone Allow/Deny
     *  for everyday-but-consequential ones (messages and shares to others, posts, deletes, native autofill,
     *  unrecognized Settings changes). Low-risk actions never get here. */
    fun level(risk: Risk): String =
        // A Settings change the phone didn't recognize fails closed (D2): never a tap.
        if (risk.money != null || (risk.kind == "fill" && risk.grant == null) || risk.kind == "settings") "fingerprint"
        else "tap"

    fun tightenPermission(perm: String, value: String): Boolean = synchronized(lock) {
        val values = permValues(perm)
        if (value !in values || values.indexOf(value) >= values.indexOf(permSetting(perm))) return false
        permDefaults[perm] = value
        true
    }

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
        ownRequests.add(GateText.sha256(text.split(WS).filter { it.isNotEmpty() }.joinToString(" ")) to now + lim("own_request_ms"))
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
        permits.clear(); pending.clear(); followup = null; credits.clear(); startPermits.clear()
        taskGrants.clear(); ownRequests.clear()
        never = null
        entries.removeAll { it.record["type"] == "never" }
        suspended = false
    }

    val promptOpen: Boolean get() = synchronized(lock) { pending.isNotEmpty() }
    fun pending(id: String): Pending? = synchronized(lock) { pending[id] }

    // --- admit ------------------------------------------------------------------------------------

    fun admit(method: String, params: Map<String, Any?>, rawScreen: GScreen): Admission = synchronized(lock) {
        val now = now()
        expire(now)
        val screen = withOcr(rawScreen)
        if (method in rules.meta) return Admission("allow", method = method, params = params)
        if (method in rules.task) return requestTask(params)
        if (method in rules.read || method.startsWith("stream_")) {
            val hidden = hidden(screen)
            // wait_for reads the same element list as ui_elements, so it is blacked out the same way.
            if ((method == "screenshot" || method == "ui_elements" || method == "wait_for") && hidden.isNotEmpty()) {
                return Admission("redact", rule = hidden, method = method, params = params)
            }
            if (method == "stream_start" && hidden.isNotEmpty()) return refuse("gate_blocked_app", hidden,
                "A protected app is on screen, so the live view can't start now.", method, params)
            return Admission("allow", method = method, params = params)
        }
        if (method !in rules.act) return refuse("gate_unknown_method", "builtin:methods", "Unknown method $method.", method, params)
        if (!screen.available) return refuse("gate_unavailable", "builtin:no_accessibility",
            "OpenMob's accessibility service is off, so the phone can't check this. Turn it on.", method, params)
        if (method != "wake_unlock" && screen.keyguardLocked && screen.keyguardSecure) {
            return refuse("phone_locked", "builtin:keyguard", "The phone is locked. Unlock it to continue.", method, params)
        }
        if (method != "wake_unlock") ownerDialog(screen)?.let {
            return refuse("needs_owner_dialog", "builtin:owner_dialog", it, method, params)
        }
        val j = judge(method, params, screen)
        j.refuse?.let { return refuse(it.first, it.second, it.third, method, params, j.pkg) }
        if (suspended) return refuse("gate_rate_limited", "builtin:suspended",
            "Paused on the phone after repeated denials. The owner can resume.", method, params)
        val bind = GateScreen.binding(method, params, if (j.risk?.money != null) j.moneyFp else j.strictFp)
        val plain = GateScreen.binding(method, params, j.strictFp)
        if (pending.isNotEmpty()) {
            // The same action asked again: the prompt already showing, not a new one.
            pending.values.firstOrNull { it.binding == bind }?.let { p ->
                return Admission("ask", code = "gate_needs_approval", rule = "risk:${p.risk.reason}", kind = p.risk.kind,
                    approvalId = p.id, wording = p.risk.wording, grantOffer = p.offer, binding = bind, money = p.risk.money,
                    plain = plain, message = "Waiting for the owner's approval on the phone.", method = method,
                    params = params, pkg = j.pkg)
            }
            return refuse("gate_prompt_open", "builtin:prompt_open", "Waiting for the owner to answer a prompt on the phone.", method, params)
        }
        if (!takeToken(now)) return refuse("gate_rate_limited", "builtin:act_rate", "Too many actions: wait a second.", method, params)
        if (method != "wake_unlock" && bind !in permits) {
            // Any other action voids every outstanding approval: what the owner saw may no longer be true (a
            // quantity "+", another item scrolled into place). The honest server re-sends at once.
            permits.clear()
            startPermits.clear()
        }
        trackFollowup(method, params, j)
        if (j.app && j.pkg.isNotEmpty()) lastApp = j.pkg
        taskGrants[params["task"]?.toString().orEmpty()]?.let { g -> if (j.app && j.pkg.isNotEmpty() && g.first.isEmpty()) g.first = j.pkg }
        val mode = mode(params["mode"] as? String)
        var risk = j.risk
        val hint = params["hint"]?.toString()?.trim().orEmpty()
        if (risk == null && hint.isNotEmpty() && mode != "never") risk = Risk("other", "server_hint", wording = j.wording.ifEmpty { wording(method, params, j, screen) })
        if (mode == "ask" && j.changing && risk == null) risk = Risk("step", "mode:ask", wording = wording(method, params, j, screen))
        if (risk == null) return Admission("allow", strict = j.payment, binding = bind, method = method, params = params, pkg = j.pkg, plain = plain)
        if (mode == "never") {
            if (risk.money != null && never?.allowMoney != true) return refuse("gate_denied", "never:money",
                "Purchases are off while agents act without asking.", method, params, j.pkg)
            if (risk.always) return refuse("gate_denied", "never:${risk.reason}",
                "Nobody can be asked while agents act without asking, so this is refused.", method, params, j.pkg)
            return Admission("allow", strict = true, binding = bind, kind = risk.kind, money = risk.money, method = method,
                params = params, pkg = j.pkg, plain = plain)
        }
        if (bind in permits) return Admission("allow", strict = true, binding = bind, permit = bind, kind = risk.kind,
            money = risk.money, method = method, params = params, pkg = j.pkg, plain = plain)
        if (mode != "ask") covered(risk, j.pkg, params)?.let {
            return Admission("allow", strict = true, binding = bind, kind = risk.kind, rule = it, money = risk.money,
                method = method, params = params, pkg = j.pkg, plain = plain)
        }
        if (mode != "ask" && taskCovers(risk, params, j.pkg, screen)) return Admission("allow", strict = true, binding = bind,
            kind = risk.kind, rule = "task_grant", method = method, params = params, pkg = j.pkg, plain = plain)
        if (mode != "ask" && everyday && level(risk) == "tap" && risk.kind != "permission" && risk.kind != "uninstall") return Admission("allow", strict = true, binding = bind,
            kind = risk.kind, rule = "everyday", method = method, params = params, pkg = j.pkg, plain = plain)
        // Task approval, just in time: the first step that needs a tap-level confirmation asks for the task instead
        // (worded from the request, with this step as the detail). A request typed on this phone needs no prompt.
        val task = params["task"]?.toString().orEmpty()
        val text = params["task_text"]?.toString().orEmpty().split(WS).filter { it.isNotEmpty() }.joinToString(" ").take(300)
        val lazy = mode != "ask" && task.isNotEmpty() && text.isNotEmpty() && level(risk) == "tap" &&
            risk.kind != "task" && risk.kind != "step" && risk.kind in taskKinds(text)
        if (lazy && ownRequests.any { it.first == GateText.sha256(text) && it.second > now }) {
            mintTask(task, GateText.sha256(text), text)
            taskGrants[task]?.let { g -> if (g.first.isEmpty()) g.first = if (j.app) j.pkg else lastApp }
            if (taskCovers(risk, params, j.pkg, screen)) return Admission("allow", strict = true, binding = bind,
                kind = risk.kind, rule = "task_grant:own_request", method = method, params = params, pkg = j.pkg, plain = plain)
        }
        val window = lim("ask_window_ms")
        asks.removeAll { now - it >= window }
        if (asks.size >= lim("asks_per_window")) {
            val wait = maxOf(1L, (asks[0] + window - now + 59_999) / 60_000)
            return refuse("gate_rate_limited", "builtin:prompt_rate",
                "Too many approval prompts on the phone: try again in $wait min.", method, params, j.pkg)
        }
        val id = "gk" + UUID.randomUUID().toString().replace("-", "") // the legacy approval path refuses gk ids
        val current = taskGrants[task]
        if (lazy && (current == null || current.digest != GateText.sha256(text) || now >= current.expires)) {
            val wording = "Agent wants to: $text. Allow the steps this needs? Next: ${risk.wording}"
            pending[id] = Pending(id, bind, now, Risk("task", "task", wording = wording), if (j.app) j.pkg else lastApp,
                task, null, method, params).also { it.digest = GateText.sha256(text); it.text = text; it.step = true; it.level = "tap" }
            onEvent?.invoke(id, "pending")
            return Admission("ask", code = "gate_needs_approval", rule = "risk:task:${risk.reason}", kind = "task",
                approvalId = id, wording = wording, binding = bind, level = "tap", plain = plain,
                message = "Waiting for the owner's approval of this task on the phone.", method = method, params = params, pkg = j.pkg)
        }
        val offer = offer(risk, mode)
        pending[id] = Pending(id, bind, now, risk, j.pkg, params["task"]?.toString().orEmpty(), offer, method, params)
            .also { it.level = level(risk) }
        onEvent?.invoke(id, "pending")
        Admission("ask", code = "gate_needs_approval", rule = "risk:${risk.reason}", kind = risk.kind, approvalId = id,
            wording = risk.wording, grantOffer = offer, binding = bind, money = risk.money, plain = plain, level = level(risk),
            message = "Waiting for the owner's approval on the phone.", method = method, params = params, pkg = j.pkg)
    }

    /** Why reads must not see this screen ("" if they may): a blocked app in any window, or an app window the
     *  phone can't identify yet (it may be a blocked app still loading). */
    // --- task approval (by owner request): one prompt for the task, its non-money risky steps then run --------

    /** The server asks for a task grant at task start: the phone shows the request itself ("Agent wants to: …")
     *  and the owner approves the task once. Requests typed or spoken on this phone need no prompt. */
    fun requestTask(params: Map<String, Any?>): Admission = synchronized(lock) {
        val now = now()
        val task = params["task"]?.toString().orEmpty()
        val text = params["text"]?.toString().orEmpty().split(WS).filter { it.isNotEmpty() }.joinToString(" ").take(300)
        val method = "task_grant"
        if (task.isEmpty() || text.isEmpty()) return refuse("gate_denied", "builtin:task_request",
            "A task grant needs the task id and its request text.", method, params)
        val digest = GateText.sha256(text)
        val bind = GateText.sha256("gk1-task|$task|$digest")
        taskGrants[task]?.let { g ->
            if (g.digest == digest && now < g.expires) return Admission("allow", rule = "task_grant", kind = "task",
                method = method, params = params, binding = bind)
        }
        if (everyday) return Admission("allow", rule = "everyday", kind = "task", method = method, params = params, binding = bind)
        if (ownRequests.any { it.first == digest && it.second > now }) {
            mintTask(task, digest, text)
            return Admission("allow", rule = "task_grant:own_request", kind = "task", method = method, params = params, binding = bind)
        }
        pending.values.firstOrNull { it.binding == bind }?.let { p ->
            return Admission("ask", code = "gate_needs_approval", rule = "risk:task", kind = "task", approvalId = p.id,
                wording = p.risk.wording, binding = bind, method = method, params = params,
                message = "Waiting for the owner's approval on the phone.")
        }
        if (pending.isNotEmpty()) return refuse("gate_prompt_open", "builtin:prompt_open",
            "Waiting for the owner to answer a prompt on the phone.", method, params)
        val window = lim("ask_window_ms")
        asks.removeAll { now - it >= window }
        if (asks.size >= lim("asks_per_window")) return refuse("gate_rate_limited", "builtin:prompt_rate",
            "Too many approval prompts on the phone: try again later.", method, params)
        val wording = "Agent wants to: $text. Allow the steps this needs?"
        val risk = Risk("task", "task", wording = wording)
        val id = "gk" + UUID.randomUUID().toString().replace("-", "")
        pending[id] = Pending(id, bind, now, risk, "", task, null, method, params).also { it.digest = digest; it.level = "tap"; it.text = text }
        onEvent?.invoke(id, "pending")
        Admission("ask", code = "gate_needs_approval", rule = "risk:task", kind = "task", approvalId = id, wording = wording,
            binding = bind, method = method, params = params, level = "tap",
            message = "Waiting for the owner's approval of this task on the phone.")
    }

    /** The grant covers what the request names: the first app the task works in plus apps the request names, and only
     *  the kinds of risky step its words ask for (a "share" request doesn't cover deleting or posting). */
    private fun mintTask(task: String, digest: String, text: String) {
        taskGrants[task] = TaskGrant(digest, now() + lim("task_grant_ms"), GateText.tokens(text), taskKinds(text))
    }

    /** The kinds of risky step a request's words ask for. */
    private fun taskKinds(text: String): Set<String> {
        val toks = GateText.tokens(text)
        val kinds = mutableSetOf("other", "fill")
        for ((kind, extra) in listOf("send" to listOf("share", "message", "text", "reply", "forward", "email", "tell"),
                                     "share" to listOf("share", "send", "forward"), "post" to listOf("share"),
                                     "delete" to listOf("unsubscribe", "clear", "remove", "empty", "uninstall"))) {
            if (GateText.anyPhrase(toks, (rules.w[kind] ?: emptyList()) + extra) != null) kinds.add(kind)
        }
        return kinds
    }

    /** Under the task's grant: the tap-level risky steps of the kinds its request asks for, in its first app or an app
     *  the request names (system surfaces such as the share sheet count as the app they serve). Never money,
     *  Settings, browser or WebView autofill, or permission dialogs (and never the hard denies). */
    private fun taskCovers(risk: Risk, params: Map<String, Any?>, pkg: String, screen: GScreen): Boolean {
        val g = taskGrants[params["task"]?.toString().orEmpty()] ?: return false
        if (now() >= g.expires || g.used >= lim("task_grant_steps")) return false
        val text = params["task_text"]?.toString().orEmpty().split(WS).filter { it.isNotEmpty() }.joinToString(" ").take(300)
        if (text.isNotEmpty() && GateText.sha256(text) != g.digest) return false // the grant was for another request
        if (risk.kind == "task" || risk.kind == "step" || level(risk) != "tap" || risk.kind !in g.kinds) return false
        var app = if (pkg.isNotEmpty() && screen.windows.any { it.pkg == pkg && appWindow(it) }) pkg else lastApp
        if (risk.reason.startsWith("uninstall:")) app = risk.grant.orEmpty() // an uninstall counts as the app it removes
        val label = GateText.tokens(screen.labels[app].orEmpty())
        if (app != g.first && !(label.isNotEmpty() && GateText.hasPhrase(g.words, label.joinToString(" ")))) return false
        g.used++
        if (pkg.isNotEmpty()) g.apps.add(pkg) // the apps the task actually used
        return true
    }

    fun hidden(screen: GScreen): String = when {
        screen.windows.any { rules.blocked(it.pkg) } -> "builtin:sensitive_apps"
        // A permission dialog Android hides from accessibility, identified (a fresh permission-controller event and
        // an OCR'd "Allow X to" title), isn't sensitive: reads show it. Any other window with no package stays black.
        screen.windows.any { w -> w.pkg.isEmpty() && w.type == "application" && w.id != ocrWindow(screen)?.id } -> "builtin:unknown_window"
        else -> ""
    }

    /** A purchase flow's one terminal follow-up ends at an app switch, at BACK/HOME/recents, a launch or a link,
     *  a tap on a cart or checkout word, and after 3 other actions. */
    private fun trackFollowup(method: String, params: Map<String, Any?>, j: Judgement) {
        val f = followup ?: return
        val name = params["name"]?.toString().orEmpty().uppercase()
        if (method == "launch_app" || method == "open_url" || (method == "key" && name in setOf("BACK", "HOME", "APP_SWITCH")) ||
            (j.app && j.pkg.isNotEmpty() && j.pkg != f.pkg) || (j.label.isNotEmpty() && has(j.label, rules.list("cart_words")))) {
            followup = null
            return
        }
        f.acts++
        if (f.acts > 3) followup = null
    }

    /** A system dialog the phone can't read is up (Android 14+ hides permission dialogs, Safety Center and
     *  default-apps screens from accessibility): only the owner can answer it. Returns what to tell them, or null. */
    fun ownerDialog(screen: GScreen): String? {
        if (ocrWindow(screen) == null) return null
        val m = PERMISSION_APP.find(screen.eventText)
            ?: return "A system dialog needs your answer: choose on your phone, then the task continues."
        val kind = permRules["kinds"].list().map { it.strings() }.firstOrNull { rules.rx(it[1]).containsMatchIn(screen.eventText) }?.get(0)
        val what = kind?.replace('_', ' ') ?: "a permission"
        return "${m.groupValues[1]} is asking for $what: choose on your phone, then the task continues."
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
        if (g != null) {
            val wanted = mutableSetOf(g)
            if (risk.kind == "permission" && g.endsWith("|once")) wanted.add(g.removeSuffix("once") + "while_using")
            if (grants.any { it.kind == risk.kind && it.binding in wanted && it.issued + it.durationMs > now }) return "grant"
        }
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
        var m = if (p.level == "tap") "gk1|tap|$device|$id|${p.binding}" else "gk1|$device|$id|${p.binding}"
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
        markShown(id) // answered, so it was seen
        val plain = approvalMessage(id, false)
        var grant: GrantOffer? = null
        if (message != plain) {
            if (p.offer == null || message != approvalMessage(id, true)) return false
            grant = p.offer
        }
        // A tap-level prompt is signed with the tap key, a fingerprint one with the screen-lock key: neither signature
        // can approve the other kind (the messages are domain-separated too).
        if (!(if (p.level == "tap") tapVerifier else verifier).verify(message, signature)) return false
        pending.remove(id)
        promptClosed = now
        if (p.risk.kind == "task") { // the owner approved the task: a grant, not a one-shot permit
            mintTask(p.task, p.digest, p.text)
            if (p.step) {
                permits[p.binding] = now + lim("permit_ms")
                taskGrants[p.task]?.let { g -> if (g.first.isEmpty()) g.first = p.pkg }
            }
            onEvent?.invoke(id, "approved")
            return true
        }
        permits[p.binding] = now + lim("permit_ms")
        if (p.risk.money == "start") startPermits[p.binding] = p.pkg to p.task // the follow-up starts when this tap runs
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

    /** The prompt is on the owner's screen: now it counts toward the prompt limit. */
    fun markShown(id: String) = synchronized(lock) {
        val p = pending[id] ?: return@synchronized
        if (!p.counted) { p.counted = true; asks.add(now()) }
    }

    fun cancel(id: String): Boolean = synchronized(lock) {
        if (pending.remove(id) == null) return false
        promptClosed = now()
        onEvent?.invoke(id, "expired")
        true
    }

    fun deny(id: String): Boolean = synchronized(lock) {
        val now = now()
        markShown(id)
        if (pending.remove(id) == null) return false
        promptClosed = now
        denials.removeAll { now - it >= lim("denial_window_ms") }
        denials.add(now)
        if (denials.size >= lim("denials_to_suspend")) suspended = true
        onEvent?.invoke(id, "denied")
        true
    }

    // --- reverify: right before the side effect, under the gesture lock (D8) ------------------------

    /** How much longer an admitted gesture waits for the window list to settle before [reverify] (B7). The list the
     *  gate reads lags the screen: an app's launch screen isn't in it at all (the previous app stays listed, and
     *  nothing has input focus), and the app's own window is listed a moment after it can take touches. So a tap
     *  judged on that list could land in a window that just appeared, such as a blocked app's. It waits while no
     *  listed window has focus (an app is starting) and until nothing has changed for [SETTLE_MS]. Never longer
     *  than the caller's deadline; then [unsettled] refuses if nothing has focus yet. */
    fun settleMs(adm: Admission, quietMs: Long, anyFocused: Boolean = true): Long = when {
        !adm.allowed || adm.method !in GESTURES -> 0L
        !anyFocused -> SETTLE_MS
        else -> maxOf(0L, SETTLE_MS - quietMs)
    }

    /** What must hold still for a risky action: the target's identity, bounds and window, numbers blanked (a
     *  live screen's odds or prices ticking elsewhere, or in its label, don't count). */
    fun stableKey(adm: Admission, screen: GScreen): String = synchronized(lock) {
        val j = judge(adm.method, adm.params, screen)
        j.refuse?.second ?: GateScreen.binding(adm.method, adm.params, j.strictFp)
    }

    /** The refusal for a gesture whose screen never settled: no window had focus by the deadline. */
    fun unsettled(adm: Admission): Admission = refuse("gate_screen_unstable", "builtin:no_focus",
        "The screen is changing (an app is opening). Look again.", adm.method, adm.params)

    /** Right before the side effect. A Precise/Approximate tap on an OCR'd dialog is only armed here: the phone
     *  records it once the gesture completed ([completed]); a refused one makes it assume Precise again (B1). */
    fun reverify(adm: Admission, rawScreen: GScreen): Admission = synchronized(lock) {
        armed = null
        val out = reverifyChecks(adm, rawScreen)
        if (adm.allowed && adm.method == "tap") {
            val card = ocrCard(adm, withOcr(rawScreen))
            if (!out.allowed) { if (card != null) accuracyChoice = null }
            else if (card != null) armed = adm.binding to card
        }
        out
    }

    /** The gesture for an admitted action completed on the screen. */
    fun completed(adm: Admission) = synchronized(lock) {
        armed?.let { (binding, card) -> if (binding == adm.binding) accuracyChoice = card }
        armed = null
    }

    private var armed: Pair<String, Pair<Int, String>>? = null

    private fun reverifyChecks(adm: Admission, rawScreen: GScreen): Admission {
        if (!adm.allowed || adm.method !in rules.act) return adm
        val screen = withOcr(rawScreen)
        // No gesture while a prompt is up, or just after one closed: an action admitted a moment before could land on
        // its Allow (B1). The approved action itself (its permit) goes ahead.
        if (adm.method != "wake_unlock" && adm.permit == null &&
            (pending.isNotEmpty() || now() - promptClosed < lim("prompt_cooldown_ms"))) {
            return refuse("gate_prompt_open", "builtin:prompt_cooldown",
                "An approval prompt is (or was just) on screen. Try again in a moment.", adm.method, adm.params, adm.pkg)
        }
        val j = judge(adm.method, adm.params, screen)
        j.refuse?.let {
            adm.permit?.let(permits::remove)
            return refuse(it.first, it.second, it.third, adm.method, adm.params, j.pkg)
        }
        if (adm.strict) {
            if (GateScreen.binding(adm.method, adm.params, if (adm.money != null) j.moneyFp else j.strictFp) != adm.binding) {
                adm.permit?.let(permits::remove)
                if (adm.money != null && GateScreen.binding(adm.method, adm.params, j.strictFp) == adm.plain) {
                    return refuse("gate_screen_changed", "builtin:price_changed",
                        "The amount or price changed since it was approved. Look again.", adm.method, adm.params, j.pkg)
                }
                return refuse("gate_screen_changed", "builtin:toctou", "The screen changed before the action ran. Look again.",
                    adm.method, adm.params, j.pkg)
            }
            if (screen.quietMs < lim("quiet_ms")) return refuse("gate_screen_unstable", "builtin:toctou",
                "The screen is still changing. Try again in a moment.", adm.method, adm.params, j.pkg)
        } else if (j.risk != null || j.payment) {
            return refuse("gate_screen_changed", "builtin:toctou", "The screen changed before the action ran. Look again.",
                adm.method, adm.params, j.pkg)
        }
        if (adm.permit != null) {
            if (permits.remove(adm.permit) == null) {
                return refuse("gate_screen_changed", "builtin:permit", "That approval was already used.", adm.method, adm.params, j.pkg)
            }
            startPermits.remove(adm.permit)?.let { (pkg, task) -> followup = Followup(pkg, task, now() + lim("purchase_followup_ms")) }
        }
        return adm
    }

    // --- permission dialogs Android hides from accessibility (Android 14+): the phone's own OCR ---------------

    /** OCR can't see which location card is chosen: the card the phone let through last, in this dialog. */
    private var accuracyChoice: Pair<Int, String>? = null

    /** The permission-controller window with no readable tree (identified by its package, or by the last
     *  window-state event when Android hides even that). */
    /** The permission-controller window with no readable tree: identified by its own package, or (when Android hides
     *  even that and sends no event either: Samsung, Android 15) by the phone's OCR of its own screenshot: an "Allow X
     *  to ..." title AND either a fresh permission-controller event or a "Don't allow" button (the dialog's shape).
     *  [needTitle] false: should the phone OCR to find out (the top window with no package and no readable tree). */
    fun ocrWindow(screen: GScreen, needTitle: Boolean = true): GWindow? {
        val perm = rules.rx(rules.sStr("permission_packages"))
        for (w in screen.ordered) {
            if (w.type != "application") continue
            if (w.root != null && walk(w.root).any { it.text.isNotEmpty() || it.desc.isNotEmpty() }) continue
            if (perm.containsMatchIn(w.pkg)) return w
            if (w.pkg.isNotEmpty()) continue
            if (!needTitle) return w
            val slack = (permRules["ocr"].obj()["event_slack_ms"] as Number).toLong()
            val fresh = perm.containsMatchIn(screen.eventPackage) && screen.eventAt >= w.since - slack
            return if (ocrTitle(screen) && (fresh || ocrDeny(screen))) w else null // only the top unreadable window
        }
        return null
    }

    private fun ocrLines(screen: GScreen): List<String> {
        val minConf = (permRules["ocr"].obj()["min_conf"] as Number).toDouble()
        return screen.ocr.orEmpty().filter { it.conf >= minConf }.map { it.text.split(WS).filter { t -> t.isNotEmpty() }.joinToString(" ") }
    }

    /** The OCR shows a permission dialog's "Don't allow" (or its translation). */
    private fun ocrDeny(screen: GScreen) = ocrLines(screen).any { textRole(it) == "deny" || textRole(it) == "deny_forever" }

    /** Should the phone OCR the screen to find out whether a hidden permission dialog is up? */
    fun ocrCandidate(screen: GScreen): GWindow? = ocrWindow(screen, needTitle = false)

    /** The phone's OCR shows a permission dialog's title ("Allow Kalshi to ...") with confidence. */
    fun ocrTitle(screen: GScreen): Boolean {
        // One line, or two in a row (a title wrapped or split where the app name is bold).
        val lines = ocrLines(screen)
        return (lines + lines.zipWithNext { a, b -> "$a $b" }).any { permApp(it) != null }
    }

    fun textRole(text: String): String? {
        val toks = GateText.tokens(text)
        if (toks.isEmpty()) return null
        for (pair in permRules["button_texts"].list()) {
            val (role, label) = pair.strings()
            if (toks == GateText.tokens(label)) return role
        }
        return null
    }

    /** The requesting app's name, from the dialog's title ("Allow Kalshi to ..."). */
    fun permApp(message: String): String? {
        for (rx in permRules["titles"].strings()) rules.rx(rx).find(message)?.let { return it.groupValues[1].trim() }
        return null
    }

    /** The screen with the hidden permission dialog rebuilt from the phone's OCR: the title as its message, each line
     *  that reads exactly as a button as a clickable node (its box padded), the rest as text. The dialog's root is
     *  clickable, so a tap anywhere else in it (a button OCR missed, a low-confidence line) asks. */
    fun withOcr(screen: GScreen): GScreen {
        val w = ocrWindow(screen)
        if (w == null) {
            accuracyChoice = null // the dialog is gone: its card choice with it
            return screen
        }
        val ocr = screen.ocr ?: return screen
        val o = permRules["ocr"].obj()
        val minConf = (o["min_conf"] as Number).toDouble()
        val pad = (o["pad"] as Number).toInt()
        val maxTitle = (o["title_lines"] as Number).toInt()
        val start = rules.rx(o.str("title_start"))
        val lines = ocr.filter { it.text.isNotBlank() && it.conf >= minConf }
            .sortedWith(compareBy({ it.bounds[1] }, { it.bounds[0] }))
        if (accuracyChoice != null && accuracyChoice?.first != w.id) accuracyChoice = null // another dialog: Precise again
        val choice = accuracyChoice
        val title = mutableListOf<String>()
        var taking = false
        val nodes = mutableListOf<GNode>()
        for (b in lines) {
            val text = b.text.split(WS).filter { it.isNotEmpty() }.joinToString(" ")
            val role = textRole(text)
            val (l, t, r, bt) = b.bounds.toList()
            if (role != null) {
                taking = false
                // OCR can't see the radio: Android pre-selects Precise.
                val checked = if (role == "precise" || role == "approximate") role == (choice?.second ?: "precise") else null
                nodes.add(GNode(text = text, id = "ocr", cls = "OCRText", bounds = intArrayOf(l - pad, t - pad, r + pad, bt + pad),
                    clickable = true, checked = checked))
                continue
            }
            if (title.isEmpty() && start.containsMatchIn(text)) taking = true
            if (taking && title.size < maxTitle) {
                title.add(text)
                taking = !text.endsWith("?")
            }
            nodes.add(GNode(text = text, cls = "OCRText", bounds = intArrayOf(l, t, r, bt)))
        }
        val message = title.joinToString(" ").ifEmpty { nodes.filter { !it.clickable }.joinToString(" ") { it.text } }.ifEmpty { "?" }
        nodes.add(0, GNode(text = message, id = "permission_message", cls = "OCRText"))
        val root = GNode(cls = "OCRDialog", bounds = w.bounds, clickable = true, children = nodes)
        val pkg = w.pkg.ifEmpty {
            if (rules.rx(rules.sStr("permission_packages")).containsMatchIn(screen.eventPackage)) screen.eventPackage
            else permRules["ocr"].obj().str("package")
        }
        val rebuilt = GWindow(w.id, w.type, w.layer, pkg, w.title, w.bounds, root, w.since)
        return screen.withWindows(screen.windows.map { if (it === w) rebuilt else it })
    }

    /** [withOcr] for reads (ui_elements). */
    fun ocrPreview(screen: GScreen): GScreen = synchronized(lock) { withOcr(screen) }

    /** (window id, precise|approximate) when this tap is on an OCR'd dialog's location card. */
    private fun ocrCard(adm: Admission, screen: GScreen): Pair<Int, String>? {
        val x = (adm.params["x"] as? Number)?.toDouble() ?: return null
        val y = (adm.params["y"] as? Number)?.toDouble() ?: return null
        val t = GateScreen.hitTest(rules, screen, x, y) ?: return null
        if (t.window.root?.cls != "OCRDialog") return null
        val role = permRole(t)
        return if (role == "precise" || role == "approximate") t.window.id to role else null
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
        if (w.pkg.isEmpty()) return Triple("gate_screen_unstable", "builtin:unknown_window",
            "That part of the screen can't be read yet (an app may still be loading). Look again.")
        if (w.pkg in rules.own) return Triple("gate_denied_page", "builtin:own_window", "That's OpenMob's own screen; agents can't touch it.")
        if (w.pkg == rules.surfStr("systemui")) {
            val title = rules.rx(rules.surfStr("prompt_titles"))
            val ids = rules.rx(rules.surfStr("prompt_ids"))
            if (title.containsMatchIn(w.title) || walk(w.root).any { ids.containsMatchIn(it.id) }) {
                return Triple("gate_denied_page", "builtin:prompt_surface",
                    "That's the lock screen or a system approval prompt; agents can't touch it.")
            }
        }
        if (w.pkg in rules.installers && !uninstallDialog(w)) return Triple("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
        if (rules.blocked(w.pkg)) return Triple("gate_blocked_app", "builtin:sensitive_apps",
            "Blocked on the phone: ${w.pkg} is a sensitive app. Don't open it another way; call done.")
        return null
    }

    private fun appWindow(w: GWindow): Boolean = w.type == "application" && w.pkg != rules.surfStr("systemui") &&
        !isSettings(w) && w.pkg !in rules.surfList("share_packages") && w.pkg !in rules.surfList("credential_packages") &&
        !rules.rx(rules.sStr("permission_packages")).containsMatchIn(w.pkg) && w.pkg !in rules.installers

    private val uninstallRules = rules.d["uninstall"].obj()

    private fun uninstallDialog(w: GWindow): Boolean {
        val rx = rules.rx(uninstallRules.str("message"))
        return walk(w.root).any { rx.containsMatchIn(it.text) }
    }

    /** The app an uninstall dialog or app-info page is about: the installed app whose label it shows (the longest
     *  match), "protected" for OpenMob or a blocked app, "" if the phone can't tell. */
    private fun uninstallTarget(w: GWindow, screen: GScreen): String {
        val texts = walk(w.root).filter { it.text.isNotEmpty() }.map { GateText.tokens(it.text) }
        if (texts.any { tx -> uninstallRules["own_labels"].strings().any { GateText.hasPhrase(tx, it) } }) return "protected"
        var best = ""
        var size = 0
        for ((pkg, lbl) in screen.labels) {
            val lt = GateText.tokens(lbl)
            if (pkg in rules.installers || pkg == w.pkg || lt.isEmpty()) continue
            if (texts.any { GateText.hasPhrase(it, lt.joinToString(" ")) }) {
                if (rules.blocked(pkg) || pkg in rules.own) return "protected"
                if (lt.size > size) { best = pkg; size = lt.size }
            }
        }
        return best
    }

    private fun isSettings(w: GWindow): Boolean {
        if (rules.settingsPkg.containsMatchIn(w.pkg)) return true
        return w.pkg == rules.sStr("gms_package") && rules.rx(rules.sStr("gms_settings_titles")).containsMatchIn(w.title)
    }

    private fun has(toks: List<String>, phrases: List<String>) = GateText.anyPhrase(toks, phrases) != null

    /** ("refuse"|"blocked"|"ask"|"allow"|"none", rule or page fingerprint). */
    private fun settingsTier(w: GWindow, t: GTarget?, screen: GScreen, label: List<String>): Pair<String, String> {
        val title = GateScreen.pageTitle(w)
        val activity = if (w.type == "application") screen.activity else ""
        if (w.pkg in rules.sList("ask_packages")) return "ask" to w.pkg // e.g. the VPN consent dialog
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
            if (has(label, rules.sList("app_info_delete"))) return "delete" to page
            if (has(label, rules.sList("app_info_allowed"))) return "allow" to ""
            return "none" to ""
        }
        if (has(title, rules.sList("ask_pages")) || has(label, rules.sList("ask_pages"))) {
            if (label.isNotEmpty() && GateText.anyPhrase(label, rules.w.getValue("navigation")) == label.joinToString(" ")) return "allow" to ""
            return "ask" to page
        }
        if (rules.rx(rules.sStr("permission_packages")).containsMatchIn(w.pkg) || GateText.hasPhrase(title, "permissions") ||
            GateText.hasPhrase(title, "permission")) {
            if (sessionAppAsks(w, screen, title)) return "allow" to ""
            return if (changingTap(t, label)) "ask" to page else "none" to ""
        }
        if (has(title, rules.sList("everyday")) || has(label, rules.sList("everyday"))) return "allow" to ""
        return if (changingTap(t, label)) "ask" to page else "none" to ""
    }

    /** Is this permission request from the app the task is using? The dialog names the app in one place
     *  ("Allow <app> to ..."); without that line, the requesting app's window must be right underneath; on a
     *  Settings permission page, its header must name the app. */
    private fun sessionAppAsks(w: GWindow, screen: GScreen, title: List<String>): Boolean {
        val session = if (lastApp.isNotEmpty()) GateText.tokens(screen.labels[lastApp].orEmpty()) else emptyList()
        if (session.isEmpty()) return false
        val message = walk(w.root).firstOrNull { it.id.endsWith("permission_message") && it.text.isNotEmpty() }?.text.orEmpty()
        if (message.isNotEmpty()) {
            val app = GateText.tokens(permApp(message) ?: return false)
            if (app.isEmpty()) return false
            if (app == session) return true
            // The dialog may name the app shorter or longer than its label ("Chess" for "Chess.com"): then the
            // requesting app's window must also be right underneath.
            val below = screen.ordered.filter { it.layer < w.layer && it.type == "application" }
            return (app == session.take(app.size) || session == app.take(session.size)) && below.isNotEmpty() && below[0].pkg == lastApp
        }
        if (rules.rx(rules.sStr("permission_packages")).containsMatchIn(w.pkg)) {
            val below = screen.ordered.filter { it.layer < w.layer && it.type == "application" }
            return below.isNotEmpty() && below[0].pkg == lastApp
        }
        return GateText.hasPhrase(title, session.joinToString(" "))
    }

    private fun permRole(t: GTarget): String? {
        for (n in listOfNotNull(t.pressed, t.hit)) if (n.id.isNotEmpty()) {
            for (pair in permRules["button_ids"].list()) {
                val (role, rx) = pair.strings()
                if (rules.rx(rx).containsMatchIn(n.id)) return role
            }
        }
        val label = t.labelTokens()
        for (pair in permRules["button_texts"].list()) {
            val (role, text) = pair.strings()
            if (label.isNotEmpty() && label == GateText.tokens(text)) return role
        }
        return null
    }

    /** A tap in a runtime permission dialog: a Risk, null, or a refusal Triple(code, rule, message). Denying (and
     *  "Approximate") is always fine. "Allow all the time" never. The owner's settings: `deny` lets only "Don't allow"
     *  through; the task's own app may then grant what they allow without a prompt (signed per-app grants first;
     *  built in: only this time, notifications allowed, location approximate); another app's request and a button the
     *  phone doesn't recognize ask (a tap-level prompt). */
    private fun permissionDialog(w: GWindow, t: GTarget, screen: GScreen): Any? {
        val message = walk(w.root).firstOrNull { it.id.endsWith("permission_message") && it.text.isNotEmpty() }?.text.orEmpty()
        val kind = permRules["kinds"].list().map { it.strings() }.firstOrNull { rules.rx(it[1]).containsMatchIn(message) }?.get(0) ?: "other"
        val role = permRole(t)
        val name = permApp(message) ?: "this app"
        if (role == null) {
            // A button the phone doesn't recognize (other ids, another language, OCR it couldn't read) may grant: ask.
            val p = t.pressed
            val changing = p != null && (p.clickable || p.longClickable || p.checked != null)
            return if (changing) Risk("permission", "permission:unknown", always = true,
                wording = "Allow $name's permission request?") else null
        }
        if (role in setOf("deny", "deny_forever", "approximate")) return null
        if (role == "always") return Triple("gate_permission_choice", "permission:always",
            "Never \"Allow all the time\": tap \"While using the app\" (or \"Only this time\") instead.")
        val what = if (role == "precise") "precise location" else kind.replace('_', ' ')
        val wording = "Allow $name to use your $what?"
        val (value, settingKind) = when {
            role == "precise" -> "precise" to "location_accuracy"
            kind == "notifications" -> "allow" to "notifications"
            else -> (if (role == "one_time") "once" else "while_using") to kind
        }
        val setting = permSetting(settingKind)
        val own = sessionAppAsks(w, screen, GateScreen.pageTitle(w))
        val grant = "$lastApp|$settingKind|$value"
        if (setting == "deny" && !(own && permGranted(grant))) return Triple("gate_permission_choice",
            "permission:$settingKind:deny", "The owner's setting for ${settingKind.replace('_', ' ')} is Don't allow: tap \"Don't allow\".")
        if (!own) return Risk("permission", "permission:other_app", always = true, wording = wording)
        var ok = when {
            role == "precise" -> setting == "precise"
            kind == "notifications" -> setting == "allow"
            else -> {
                // "Only this time" by default; where the dialog doesn't offer it, the next narrowest grant.
                val onlyOnce = walk(w.root).any { it.clickable && permRole(GTarget(w, listOf(it))) == "one_time" }
                // ...except a lasting grant of a dangerous kind (contacts, SMS…): it asks unless the owner signed
                // persistent_grants: allow.
                val lasting = kind in permRules["persistent_ask"].strings() && permSetting("persistent_grants") != "allow"
                setting == "while_using" || (setting == "once" && (role == "one_time" || (!onlyOnce && !lasting)))
            }
        }
        ok = ok || permGranted(grant)
        if (ok && kind == "location" && permSetting("location_accuracy") != "precise" && preciseSelected(w) &&
            !permGranted("$lastApp|location_accuracy|precise")) {
            // Android pre-selects Precise: granting now would give precise location against the owner's setting.
            return Triple("gate_permission_choice", "permission:location_accuracy",
                "The owner allows approximate location only: tap \"Approximate\" first, then grant.")
        }
        if (ok) return null
        return Risk("permission", "permission:$settingKind", always = true, grant = grant, wording = wording)
    }

    /** A signed per-app permission grant ("Always allow" on an earlier prompt). */
    private fun permGranted(grant: String): Boolean {
        val wanted = mutableSetOf(grant)
        if (grant.endsWith("|once")) wanted.add(grant.removeSuffix("once") + "while_using")
        val now = now()
        return grants.any { it.kind == "permission" && it.binding in wanted && it.issued + it.durationMs > now }
    }

    private fun preciseSelected(w: GWindow): Boolean =
        walk(w.root).any { it.checked == true && permRole(GTarget(w, listOf(it))) == "precise" }

    /** The phone has an input focus it couldn't read: typing or ENTER would go somewhere unjudged (review R1). */
    private fun focusUnread(screen: GScreen): Triple<String, String, String>? =
        if (screen.focusExpected) Triple("gate_screen_unstable", "builtin:focus_unread",
            "The field that has focus couldn't be read. Look again.") else null

    private fun changingTap(t: GTarget?, label: List<String>): Boolean {
        if (t != null && listOfNotNull(t.pressed, t.hit).any { it.checked != null }) return true
        return has(label, rules.sList("change_words"))
    }

    private fun wordRisk(label: List<String>, ids: List<String>, pkg: String, payment: Boolean, priced: Boolean = true): Risk? {
        val words = GateText.stripPhrases(label, rules.w.getValue("pre_commit") + rules.w.getValue("reversible"))
        val idWords = mutableListOf<String>()
        val risky = rules.list("resource_id_risky")
        val pre = rules.list("resource_id_pre_commit")
        for (rid in ids) {
            val toks = GateText.idTokens(rid)
            for (w in risky) if (w !in pre && (GateText.hasPhrase(toks, w) || (' ' !in w && w in toks))) idWords.add(w)
        }
        val moneyCtx = payment || rules.moneyApp.containsMatchIn(pkg)
        val flow = GateText.anyPhrase(words, rules.w.getValue("flow_start")) ?: idWords.firstOrNull { it in setOf("buy", "purchase", "transfer") }
        // YouTube's free Subscribe, a free group's Join: money only with a price nearby or a billing sheet.
        if (flow != null && !((flow == "subscribe" || flow == "join") && !(priced || moneyCtx))) return Risk("pay", "words:$flow", money = "start", grant = pkg)
        val terminal = GateText.anyPhrase(words, rules.w.getValue("terminal"))
            ?: idWords.firstOrNull { it in setOf("pay", "place order", "placeorder", "confirm") }
        if (terminal != null) {
            if ((terminal == "confirm" || terminal == "submit") && !moneyCtx) return Risk("other", "words:$terminal")
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
        // No window there that the phone knows of: it can't judge the touch.
        val t = GateScreen.hitTest(rules, screen, x, y) ?: return Judgement(changing = true,
            refuse = Triple("gate_screen_unstable", "builtin:no_window", "The phone can't see what's there. Look again."))
        val w = t.window
        val j = Judgement(pkg = w.pkg, fp = t.fp(false), strictFp = t.fp(true), changing = true, app = appWindow(w))
        j.moneyFp = j.strictFp + "|$" + GateScreen.moneyAmounts(w, t).joinToString(",")
        surfaceRefusal(w)?.let { j.refuse = it; return j }
        val pressed = t.pressed
        if (pressed != null && (pressed.editable || "EditText" in pressed.cls)) j.changing = false
        val label = t.labelTokens()
        j.label = label
        val ids = t.ids()
        j.payment = GateScreen.paymentContext(rules, screen)
        val shown = t.shown()
        val appLabel = label(screen, w.pkg)
        val source = lastApp.ifEmpty { w.pkg }
        val inBrowser = rules.isCat("browser", source) || t.inWebView()
        fun wording() = if (shown.isNotEmpty()) "Allow tapping “$shown” in $appLabel?" else "Allow tapping an unlabeled spot in $appLabel?"
        j.wording = wording()
        if (rules.rx(rules.sStr("permission_packages")).containsMatchIn(w.pkg)) {
            // A runtime permission dialog: its own question ("Allow Chess.com to take pictures and record video?").
            walk(w.root).firstOrNull { it.id.endsWith("permission_message") && it.text.isNotEmpty() }?.let { j.wording = it.text }
        }

        if (rules.rx(rules.sStr("permission_packages")).containsMatchIn(w.pkg) &&
            walk(w.root).any { it.id.endsWith("permission_message") && it.text.isNotEmpty() }) {
            when (val res = permissionDialog(w, t, screen)) {
                is Triple<*, *, *> -> j.refuse = Triple(res.first as String, res.second as String, res.third as String)
                else -> j.risk = res as Risk?
            }
            return j
        }
        var tier = "none" to ""
        if (isSettings(w)) {
            tier = settingsTier(w, t, screen, label)
            if (tier.first == "refuse") { j.refuse = Triple("gate_denied_page", tier.second, "This Settings page is security-critical; agents can't change it."); return j }
            if (tier.first == "blocked") { j.refuse = Triple("gate_blocked_app", tier.second, "That's a blocked app's settings; agents can't touch it."); return j }
            if (tier.first == "delete" || (tier.first == "allow" && has(label, listOf("uninstall")))) {
                val target = uninstallTarget(w, screen)
                if (target == "protected") {
                    j.refuse = Triple("gate_denied_page", "builtin:uninstall_protected",
                        "OpenMob and blocked apps can't be uninstalled or changed by agents.")
                    return j
                }
                if (tier.first == "delete") { // Disable, Clear storage: a delete-kind step on that app (tap level)
                    j.risk = if (target.isNotEmpty()) Risk("delete", "uninstall:app_info", grant = target, wording = wording())
                        else Risk("uninstall", "uninstall:unknown", wording = wording())
                    return j
                }
                return j // Uninstall: opens the installer's confirmation, which asks
            }
        }
        // The package installer's uninstall confirmation (installing is refused above): a delete-kind step.
        if (w.pkg in rules.installers) {
            val target = uninstallTarget(w, screen)
            if (target == "protected") {
                j.refuse = Triple("gate_denied_page", "builtin:uninstall_protected", "OpenMob and blocked apps can't be uninstalled by agents.")
                return j
            }
            if (label.isNotEmpty() && uninstallRules["confirm"].strings().any { label.joinToString(" ") == GateText.tokens(it).joinToString(" ") }) {
                val name = if (target.isNotEmpty()) label(screen, target) else "an app"
                j.risk = if (target.isNotEmpty()) Risk("delete", "uninstall:installer", grant = target, wording = "Allow uninstalling $name?")
                    else Risk("uninstall", "uninstall:unknown", wording = "Allow uninstalling an app?")
            }
            return j
        }
        val surface = w.type == "input_method" || w.pkg in rules.surfList("credential_packages") ||
            (screen.autofillService.isNotEmpty() && w.pkg == screen.autofillService) ||
            (w.pkg == rules.sStr("gms_package") && rules.rx(rules.surfStr("credential_titles")).containsMatchIn(w.title))
        if (surface || has(label, rules.w.getValue("credential"))) {
            // What gets filled is the focused field: grants bind to its app, never to a browser or WebView page.
            val field = GateScreen.focusedField(screen)
            val filled = field?.window?.pkg.orEmpty()
            val web = field != null && (rules.isCat("browser", filled) || field.path.any { "WebView" in it.cls })
            val grant = if (filled.isEmpty() || web || rules.blocked(screen.autofillService)) null else filled
            j.risk = Risk("fill", "autofill", always = true, grant = grant,
                wording = "Fill your saved card or password into ${label(screen, filled.ifEmpty { source })}?")
            return j
        }
        if (w.pkg in rules.surfList("share_packages") &&
            (rules.rx(rules.surfStr("share_titles")).containsMatchIn(w.title) || w.pkg != "android")) {
            // What is shared, to whom: a public link to yourself, or copying it, is low risk.
            val link = GateScreen.sharesLink(w)
            if (has(label, rules.w.getValue("copy")) || (link && GateScreen.isSelf(rules, label, screen))) return j
            j.risk = Risk("share", "share:" + (if (link) "link" else "content"), grant = "$source>${label.joinToString(" ")}",
                wording = "Share to “${shown.ifEmpty { "this target" }}”?")
            return j
        }
        if (w.pkg == rules.surfStr("systemui") && pressed != null && pressed !== w.root &&
            (rules.rx(rules.surfStr("notification_action_ids")).containsMatchIn(pressed.id) || "Button" in pressed.cls)) {
            j.risk = Risk("other", "notification_action", wording = wording())
            return j
        }
        var risk = wordRisk(label, ids, w.pkg, j.payment, GateScreen.priced(screen, w))
        // A message to yourself ("Note to self", your own chat): nobody else receives it.
        // An in-app share list's "Send" next to your own name, while a public link is shared.
        if (risk != null && risk.kind == "send" && risk.money == null && GateScreen.sharesLink(w) &&
            GateScreen.isSelf(rules, GateScreen.rowLabel(t), screen)) risk = null
        if (risk == null && (sliderAt(t) || has(label, rules.w.getValue("slide")))) {
            risk = Risk(if (j.payment) "pay" else "other", "slider", money = if (j.payment) "terminal" else null,
                grant = if (j.payment) w.pkg else null)
        }
        if (risk == null && t.path.lastOrNull()?.truncated == true) risk = Risk("other", "truncated") // couldn't read it: ask
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
            moneyFp = ends.joinToString("#") { t -> t?.let { it.fp(true) + "|$" + GateScreen.moneyAmounts(it.window, it).joinToString(",") }.orEmpty() },
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
        val f = GateScreen.focusedField(screen) ?: return Judgement(changing = true, refuse = focusUnread(screen))
        val w = f.window
        val node = f.node
        val fp = "${w.id}|${w.pkg}|${node.id}/${node.hint}/${node.cls}/${if (node.password) 1 else 0}"
        val j = Judgement(pkg = w.pkg, fp = fp, strictFp = fp + "/" + node.bounds.joinToString(","), changing = true, app = appWindow(w),
            moneyFp = fp + "/" + node.bounds.joinToString(",") + "|$" + GateScreen.moneyAmounts(w, null).joinToString(","))
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
        val f = GateScreen.focusedField(screen) ?: return Judgement(changing = name == "ENTER", refuse = focusUnread(screen))
        val w = f.window
        val node = f.node
        val fp = "${w.id}|${w.pkg}|${node.id}/${node.hint}/${node.cls}"
        val j = Judgement(pkg = w.pkg, fp = fp, strictFp = fp + "/" + node.bounds.joinToString(","), changing = name == "ENTER", app = appWindow(w),
            moneyFp = fp + "/" + node.bounds.joinToString(",") + "|$" + GateScreen.moneyAmounts(w, null).joinToString(","))
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
        private val WS = Regex("\\s+")
        private val PERMISSION_APP = Regex("(?i)^allow\\s+(.+?)\\s+to\\s")
        val RANK = mapOf("ask" to 0, "plan" to 1, "auto" to 1, "trusted" to 2, "never" to 3)
        /** Well over the accessibility service's notificationTimeout (100 ms), the event delay the list lags by. */
        const val SETTLE_MS = 250L
        val GESTURES = setOf("tap", "long_press", "swipe")

        /** A record's canonical text: sorted key=value pairs (booleans as 1/0). */
        fun canon(record: Map<String, Any?>): String = record.filterValues { it != null }.toSortedMap().entries
            .joinToString(";") { (k, v) -> "$k=" + when (v) { is Boolean -> if (v) "1" else "0"; else -> v.toString() } }
    }
}
