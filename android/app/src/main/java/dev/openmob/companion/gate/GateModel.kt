package dev.openmob.companion.gate

import java.security.MessageDigest
import java.text.Normalizer
import java.util.Locale

/*
 * The gatekeeper's view of the screen and its rules (docs/design/phone-gatekeeper.md §2.2). Plain Kotlin:
 * GateScreens.kt builds these from the accessibility tree on the phone, and the JVM tests build them from the
 * shared fixtures (tests/fixtures/gatekeeper). Mirrors tests/gatekeeper_ref.py line for line.
 */

object GateText {
    private val MARKS = Regex("\\p{Mn}+")
    private val SPLIT = Regex("[^\\p{L}\\p{N}]+")
    private val CAMEL = Regex("([a-z])([A-Z])")

    /** NFKD, diacritics stripped, lower-cased. */
    fun norm(text: String?): String =
        MARKS.replace(Normalizer.normalize(text ?: "", Normalizer.Form.NFKD), "").lowercase(Locale.ROOT)

    fun tokens(text: String?): List<String> = SPLIT.split(norm(text)).filter { it.isNotEmpty() }

    /** "com.x:id/buyNowButton" -> [buy, now, button]. */
    fun idTokens(resourceId: String?): List<String> =
        tokens(CAMEL.replace((resourceId ?: "").substringAfterLast('/'), "$1 $2"))

    fun hasPhrase(toks: List<String>, phrase: String): Boolean {
        val p = tokens(phrase)
        if (p.isEmpty()) return false
        for (i in 0..toks.size - p.size) if (toks.subList(i, i + p.size) == p) return true
        return false
    }

    fun stripPhrases(toks: List<String>, phrases: List<String>): List<String> {
        val out = toks.toMutableList()
        for (phrase in phrases.sortedByDescending { tokens(it).size }) {
            val p = tokens(phrase)
            if (p.isEmpty()) continue
            var i = 0
            while (i <= out.size - p.size) {
                if (out.subList(i, i + p.size) == p) repeat(p.size) { out.removeAt(i) } else i++
            }
        }
        return out
    }

    fun anyPhrase(toks: List<String>, phrases: List<String>): String? = phrases.firstOrNull { hasPhrase(toks, it) }

    private val VOLATILE = Regex("\\d+(?:[.,:]\\d+)*")
    private val AMOUNT = Regex("[$€£¥₹]\\s?\\d+(?:[.,]\\d+)*|\\d+(?:[.,]\\d+)*\\s?[¢€$£]")
    private val SPACE = Regex("\\s+")
    val MONEY_LABELS = listOf("total", "cost", "price", "amount", "subtotal", "you pay", "pay", "order total", "payout")

    /** A label with its volatile numbers (odds, prices, counts, times) blanked: the same element across reads. */
    fun vol(text: String?): String = VOLATILE.replace(text ?: "", "#")

    fun amounts(text: String?): List<String> = AMOUNT.findAll(text ?: "").map { SPACE.replace(it.value, "") }.toList()

    fun sha256(text: String): String = sha256(text.toByteArray(Charsets.UTF_8))

    fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private val SCHEME = Regex("^([A-Za-z][A-Za-z0-9+.\\-]*):")
    private val URL_LIKE = Regex("^(www\\.)?[\\w-]+(\\.[\\w-]+)+(/|$|:\\d)")

    fun urlScheme(url: String?): String? {
        var u = (url ?: "").replace(Regex("[\\t\\n\\r]"), "")
        u = u.replace(Regex("^[\\x00-\\x20\\u00a0\\ufeff]+"), "")
        return SCHEME.find(u)?.groupValues?.get(1)?.lowercase(Locale.ROOT)
    }

    fun looksLikeUrl(text: String?): Boolean {
        val t = (text ?: "").trim()
        if (t.isEmpty() || ' ' in t) return false
        return urlScheme(t) != null || URL_LIKE.containsMatchIn(t)
    }
}

class GateRules(val d: Map<String, Any?>) {
    private val methods = d["methods"].obj()
    val meta = methods["meta"].strings().toSet()
    val read = methods["read"].strings().toSet()
    val act = methods["act"].strings().toSet()
    val own = d["own_packages"].strings().toSet()
    private val sensitive = d["sensitive_apps"].strings().map { Regex(it) }
    val installers = d["installer_packages"].strings().toSet()
    val moneyApp = Regex(d.str("money_app"))
    private val cats = d["categories"].obj().mapValues { Regex(it.value.toString()) }
    val w = d["words"].obj().mapValues { it.value.strings() }
    val s = d["settings"].obj()
    val settingsPkg = Regex(s.str("packages"))
    val surf = d["surfaces"].obj()
    val limits = d["limits"].obj()
    val swipe = d["swipe"].obj()
    private val language = d["language"].obj()
    val lexicon = language.str("lexicon").split(' ').filter { it.isNotEmpty() }.toSet()
    val threshold = language.dbl("threshold", 0.6)
    val minTokens = language.long("min_tokens", 6)
    private val regexCache = HashMap<String, Regex>()

    fun rx(pattern: String): Regex = synchronized(regexCache) { regexCache.getOrPut(pattern) { Regex(pattern) } }
    fun rxKey(key: String): Regex = rx(d.str(key))
    fun sStr(key: String): String = s.str(key)
    fun sList(key: String): List<String> = s[key].strings()
    fun surfStr(key: String): String = surf.str(key)
    fun surfList(key: String): List<String> = surf[key].strings()
    fun limit(key: String): Long = limits.long(key)
    fun list(key: String): List<String> = d[key].strings()
    val schemesAllowed = d["schemes"].obj()["allowed"].strings()
    val schemesTypedRefused = d["schemes"].obj()["typed_refused"].strings()
    val version: Long = d.long("version")

    fun blocked(pkg: String?): Boolean {
        val p = (pkg ?: "").lowercase(Locale.ROOT)
        return p.isNotEmpty() && sensitive.any { it.containsMatchIn(p) }
    }

    fun isCat(cat: String, pkg: String?): Boolean = !pkg.isNullOrEmpty() && cats[cat]?.containsMatchIn(pkg) == true

    companion object {
        fun parse(json: String) = GateRules(MiniJson.parse(json).obj())

        /** The built-in rules shipped in the APK (a Java resource, also on the JVM test classpath). */
        fun builtIn(): GateRules = GateRules::class.java.getResourceAsStream("/gatekeeper/rules.json")!!
            .bufferedReader(Charsets.UTF_8).use { parse(it.readText()) }
    }
}

class GNode(
    val text: String = "", val desc: String = "", val id: String = "", val cls: String = "",
    val bounds: IntArray = IntArray(4), val clickable: Boolean = false, val longClickable: Boolean = false,
    val editable: Boolean = false, val password: Boolean = false, val hint: String = "", val checked: Boolean? = null,
    val focused: Boolean = false, val children: List<GNode> = emptyList(),
    /** The phone stopped reading below this node (its children aren't all here). */
    val truncated: Boolean = false,
) {
    fun contains(x: Double, y: Double) = bounds[0] <= x && x < bounds[2] && bounds[1] <= y && y < bounds[3]
    fun fp(withBounds: Boolean): String {
        val parts = mutableListOf(id, GateText.vol(text), GateText.vol(desc), cls)
        if (withBounds) parts.add(bounds.joinToString(","))
        return parts.joinToString("/")
    }
    val clicks get() = clickable || longClickable

    companion object {
        fun of(d: Map<String, Any?>): GNode = GNode(
            text = d.str("text"), desc = d.str("desc"), id = d.str("id"), cls = d.str("cls"),
            bounds = d["bounds"].list().map { (it as Number).toInt() }.toIntArray().let { if (it.size == 4) it else IntArray(4) },
            clickable = d.bool("clickable"), longClickable = d.bool("long_clickable"), editable = d.bool("editable"),
            password = d.bool("password"), hint = d.str("hint"), checked = d["checked"] as? Boolean,
            focused = d.bool("focused"), children = d["children"].list().map { of(it.obj()) },
            truncated = d.bool("truncated"),
        )
    }
}

class GWindow(
    val id: Int, val type: String, val layer: Int, val pkg: String, val title: String, val bounds: IntArray,
    val root: GNode?,
) {
    companion object {
        fun of(d: Map<String, Any?>) = GWindow(
            d.long("id").toInt(), d.str("type", "application"), d.long("layer").toInt(), d.str("package"),
            d.str("title"), d["bounds"].list().map { (it as Number).toInt() }.toIntArray().let {
                if (it.size == 4) it else intArrayOf(0, 0, 1080, 2400)
            }, d["root"]?.let { GNode.of(it.obj()) },
        )
    }
}

/** `resolve`: url -> (handler package or "", candidates), filled on the phone from PackageManager. */
class GScreen(
    val windows: List<GWindow>, val keyguardLocked: Boolean = false, val keyguardSecure: Boolean = true,
    val quietMs: Long = 1000, val activity: String = "", val autofillService: String = "",
    val resolve: Map<String, Pair<String, List<String>>> = emptyMap(), val labels: Map<String, String> = emptyMap(),
    /** The phone has a live input focus (typing and ENTER go there), read or not. */
    val focusExpected: Boolean = false,
    /** That focus resolved directly (findFocus): its window id, the node, and its ancestors (outermost first). */
    val focusWindow: Int? = null, val focusNode: GNode? = null, val focusAncestors: List<GNode> = emptyList(),
    /** The accessibility service is bound (else the phone can't see the screen at all). */
    val available: Boolean = true,
    /** The last window-state event: its package and text (they arrive even for windows Android hides). */
    val eventPackage: String = "", val eventText: String = "",
) {
    val ordered: List<GWindow> get() = windows.sortedByDescending { it.layer }

    /** The same screen with [ms] of quiet: how long the target itself has been still (GateHost.reverify). */
    fun withQuiet(ms: Long) = GScreen(windows, keyguardLocked, keyguardSecure, ms, activity, autofillService, resolve, labels,
        focusExpected, focusWindow, focusNode, focusAncestors, available, eventPackage, eventText)

    companion object {
        fun of(d: Map<String, Any?>): GScreen {
            val kg = d["keyguard"].obj()
            return GScreen(
                windows = d["windows"].list().map { GWindow.of(it.obj()) },
                keyguardLocked = kg.bool("locked"), keyguardSecure = kg["secure"] as? Boolean ?: true,
                quietMs = d.long("quiet_ms", 1000), activity = d.str("activity"),
                autofillService = d.str("autofill_service"),
                resolve = d["resolve"].obj().mapValues { (_, v) ->
                    val o = v.obj()
                    o.str("package") to o["candidates"].strings()
                },
                labels = d["labels"].obj().mapValues { it.value.toString() },
                focusExpected = d["focus"].obj().bool("expected"),
                focusWindow = (d["focus"].obj()["window"] as? Number)?.toInt(),
                focusNode = d["focus"].obj()["node"]?.let { GNode.of(it.obj()) },
                focusAncestors = d["focus"].obj()["ancestors"].list().map { GNode.of(it.obj()) },
                available = d["accessibility"] != false,
                eventPackage = d["event"].obj().str("package"), eventText = d["event"].obj().str("text"),
            )
        }
    }
}

fun walk(node: GNode?, out: MutableList<GNode> = mutableListOf(), limit: Int = 2000): MutableList<GNode> {
    if (node == null || out.size >= limit) return out
    out.add(node)
    for (c in node.children) walk(c, out, limit)
    return out
}

class GTarget(val window: GWindow, val path: List<GNode>) {
    val hit: GNode? get() = path.lastOrNull()
    val pressed: GNode? get() = path.lastOrNull { it.clicks } ?: hit

    fun labelTokens(): List<String> {
        val parts = mutableListOf<String>()
        val p = pressed
        val h = hit
        if (p != null && p.clicks) for (n in walk(p, limit = 40)) { parts.add(n.text); parts.add(n.desc) }
        else if (p != null) { parts.add(p.text); parts.add(p.desc) }
        if (h != null && h !== p) { parts.add(h.text); parts.add(h.desc) }
        return GateText.tokens(parts.filter { it.isNotEmpty() }.joinToString(" "))
    }

    fun ids(): List<String> {
        val out = mutableListOf<String>()
        for (n in listOf(pressed, hit)) if (n != null && n.id.isNotEmpty() && n.id !in out) out.add(n.id)
        return out
    }

    fun labeled() = labelTokens().isNotEmpty() || ids().isNotEmpty()
    fun inWebView() = path.any { "WebView" in it.cls }

    fun shown(): String {
        for (n in listOf(pressed, hit)) if (n != null && (n.text.isNotEmpty() || n.desc.isNotEmpty())) {
            return n.text.ifEmpty { n.desc }.trim()
        }
        pressed?.let { p ->
            for (n in walk(p, limit = 40)) if (n.text.isNotEmpty() || n.desc.isNotEmpty()) return n.text.ifEmpty { n.desc }.trim()
        }
        val ids = ids()
        return if (ids.isEmpty()) "" else GateText.idTokens(ids[0]).joinToString(" ")
    }

    fun fp(withBounds: Boolean): String =
        listOf(window.id.toString(), window.pkg, pressed?.fp(withBounds) ?: "", hit?.fp(withBounds) ?: "").joinToString("|")
}

object GateScreen {
    fun contains(b: IntArray, x: Double, y: Double) = b[0] <= x && x < b[2] && b[1] <= y && y < b[3]

    /** The window that catches a touch at (x, y) (highest layer first; our own keep-awake overlay never catches
     *  touches), and the path to the deepest node there. */
    fun hitTest(rules: GateRules, screen: GScreen, x: Double, y: Double): GTarget? {
        for (w in screen.ordered) {
            if (w.type == "accessibility_overlay" && w.pkg in rules.own) continue
            if (!contains(w.bounds, x, y)) continue
            val path = mutableListOf<GNode>()
            var node = w.root
            while (node != null && node.contains(x, y)) {
                path.add(node)
                node = node.children.lastOrNull { it.contains(x, y) }
            }
            return GTarget(w, path)
        }
        return null
    }

    class Field(val window: GWindow, val node: GNode, val path: List<GNode>)

    fun focusedField(screen: GScreen): Field? {
        for (w in screen.ordered) findFocused(w.root, emptyList())?.let { return Field(w, it.last(), it) }
        // Not in the walked tree (e.g. a Jetpack Compose text field): the node the phone's live input focus resolves
        // to, with its parent chain, counts if its window is one the phone listed.
        val node = screen.focusNode ?: return null
        val w = screen.windows.firstOrNull { it.id == screen.focusWindow } ?: return null
        return Field(w, node, screen.focusAncestors + node)
    }

    private fun findFocused(node: GNode?, path: List<GNode>): List<GNode>? {
        if (node == null) return null
        val p = path + node
        if (node.focused && (node.editable || node.password)) return p
        for (c in node.children) findFocused(c, p)?.let { return it }
        return null
    }

    fun screenText(screen: GScreen, types: Set<String>? = null): String {
        val parts = mutableListOf<String>()
        for (w in screen.windows) if (types == null || w.type in types) for (n in walk(w.root)) {
            if (n.text.isNotEmpty()) parts.add(n.text)
            if (n.desc.isNotEmpty()) parts.add(n.desc)
        }
        return parts.joinToString(" ")
    }

    fun paymentContext(rules: GateRules, screen: GScreen): Boolean {
        val field = rules.rxKey("payment_field")
        for (w in screen.windows) for (n in walk(w.root)) {
            if ((n.editable || "EditText" in n.cls) && (field.containsMatchIn(n.id) || field.containsMatchIn(n.hint))) return true
        }
        val text = screenText(screen)
        if (rules.rxKey("currency").containsMatchIn(text)) {
            return GateText.anyPhrase(GateText.tokens(text), rules.list("payment_words")) != null
        }
        return false
    }

    private val NOT_LETTERS = Regex("[^\\p{L}]+")

    /** Is the screen in a language the shipped lexicon covers? By script (mostly non-Latin letters: no), then by
     *  real words only: lower-case alphabetic words of 4+ letters. Ticker labels ("DUN · 42%", "YES 63¢"),
     *  abbreviations and names don't count. */
    fun languageCovered(rules: GateRules, screen: GScreen): Boolean {
        val parts = mutableListOf<String>()
        for (w in screen.windows) if (w.type == "application" || w.type == "system") for (n in walk(w.root)) parts.add("${n.text} ${n.desc}")
        val text = parts.joinToString(" ")
        val letters = GateText.norm(text).filter { it.isLetter() }
        if (letters.length >= 20 && letters.count { it !in 'a'..'z' }.toDouble() / letters.length >= 0.4) return false
        val words = NOT_LETTERS.split(text).filter { it.length >= 4 && it.all(Char::isLetter) && it == it.lowercase() }
            .map { GateText.norm(it) }
        if (words.size < rules.minTokens) return true
        val known = words.count { it in rules.lexicon }
        return known.toDouble() / words.size >= rules.threshold
    }

    /** The currency amounts a money commit is about: in the target's own label, and on the window's total, cost,
     *  price or amount lines. A permit for a money commit binds them: any change asks again. */
    fun moneyAmounts(window: GWindow, target: GTarget?): List<String> {
        val texts = mutableListOf<String>()
        if (target != null) {
            for (n in listOfNotNull(target.pressed) + walk(target.pressed, limit = 40).drop(1) + listOfNotNull(target.hit)) {
                texts.add(n.text); texts.add(n.desc)
            }
        }
        for (n in walk(window.root)) {
            val line = "${n.text} ${n.desc}"
            if (GateText.amounts(line).isNotEmpty() && GateText.anyPhrase(GateText.tokens(line), GateText.MONEY_LABELS) != null) texts.add(line)
        }
        return texts.flatMap { GateText.amounts(it) }.toSortedSet().toList()
    }

    /** What a Settings page calls itself: its window title, and headers near the top that aren't rows. */
    fun pageTitle(window: GWindow): List<String> {
        val parts = mutableListOf(window.title)
        val top = window.bounds[1] + (window.bounds[3] - window.bounds[1]) * 0.16
        fun visit(n: GNode, clickableAbove: Boolean) {
            val inside = clickableAbove || n.clickable
            if (!inside && !n.editable && n.bounds[1] < top && (n.text.isNotEmpty() || n.desc.isNotEmpty())) {
                parts.add(n.text.ifEmpty { n.desc })
            }
            for (c in n.children) visit(c, inside)
        }
        window.root?.let { visit(it, false) }
        return GateText.tokens(parts.filter { it.isNotEmpty() }.joinToString(" "))
    }

    val ACTION_PARAMS = listOf("x", "y", "x1", "y1", "x2", "y2", "duration_ms", "text", "name", "package", "url")

    /** What a permit binds: the method, its action params and the target's fingerprint. */
    fun binding(method: String, params: Map<String, Any?>, fp: String): String {
        val parts = mutableListOf<String>()
        for (k in ACTION_PARAMS) {
            val v = params[k] ?: continue
            val s = when (v) {
                is Boolean -> if (v) "true" else "false"
                is Number -> Math.rint(v.toDouble()).toLong().toString()
                else -> v.toString()
            }
            parts.add("$k=$s")
        }
        return GateText.sha256("gk1-action|$method|${parts.joinToString("&")}|$fp")
    }
}
