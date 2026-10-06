package dev.openmob.companion.gate

import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Rect
import android.net.Uri
import android.os.SystemClock
import android.provider.Settings
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import dev.openmob.companion.OpenMobAccessibilityService

/**
 * What the gatekeeper sees (docs/design/phone-gatekeeper.md §2.2), read from the accessibility tree on act
 * calls only: every window in z-order with its node tree (visible nodes, bounded), the keyguard, how long the
 * screen has been quiet, and, for launches and links, the app Android would open (PackageManager), never what
 * the server says.
 */
object GateScreens {
    private const val MAX_NODES = 1500
    private const val MAX_DEPTH = 60

    fun typeName(type: Int) = when (type) {
        AccessibilityWindowInfo.TYPE_APPLICATION -> "application"
        AccessibilityWindowInfo.TYPE_INPUT_METHOD -> "input_method"
        AccessibilityWindowInfo.TYPE_SYSTEM -> "system"
        AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY -> "accessibility_overlay"
        AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER -> "split_screen_divider"
        else -> "magnification_overlay"
    }

    /** Just the packages showing in any window (for reads: is a blocked app visible anywhere?). */
    fun windowPackages(service: OpenMobAccessibilityService): GScreen = GScreen(
        windows = runCatching { service.windows }.getOrDefault(emptyList()).mapNotNull { w ->
            val root = w.root ?: return@mapNotNull null
            val pkg = root.packageName?.toString().orEmpty()
            @Suppress("DEPRECATION") root.recycle()
            GWindow(w.id, typeName(w.type), w.layer, pkg, w.title?.toString().orEmpty(), IntArray(4), null)
        },
    )

    fun capture(service: OpenMobAccessibilityService, urls: List<String> = emptyList(), labelsFor: Collection<String> = emptyList()): GScreen {
        val windows = mutableListOf<GWindow>()
        val budget = intArrayOf(MAX_NODES)
        for (w in runCatching { service.windows }.getOrDefault(emptyList())) {
            val root = w.root
            val bounds = Rect().also(w::getBoundsInScreen)
            val pkg = root?.packageName?.toString().orEmpty()
            val tree = root?.let { node(it, budget, 0, service) }
            windows.add(GWindow(w.id, typeName(w.type), w.layer, pkg, w.title?.toString().orEmpty(),
                intArrayOf(bounds.left, bounds.top, bounds.right, bounds.bottom), tree))
        }
        val keyguard = service.getSystemService(android.app.KeyguardManager::class.java)
        val pm = service.packageManager
        val pkgs = (windows.map { it.pkg } + labelsFor + GateHost.lastAppOrEmpty()).filter { it.isNotEmpty() }.toSet()
        return GScreen(
            windows = windows,
            keyguardLocked = keyguard.isKeyguardLocked, keyguardSecure = keyguard.isKeyguardSecure,
            quietMs = SystemClock.uptimeMillis() - service.lastChangeAt,
            activity = service.currentActivity.orEmpty(),
            autofillService = runCatching {
                Settings.Secure.getString(service.contentResolver, "autofill_service")?.substringBefore('/')
            }.getOrNull().orEmpty(),
            resolve = urls.associateWith { resolve(pm, it) },
            labels = labels(pm, pkgs) + GateHost.blockedLabels(pm),
        )
    }

    private fun node(n: AccessibilityNodeInfo, budget: IntArray, depth: Int, service: OpenMobAccessibilityService): GNode? {
        if (budget[0] <= 0 || depth > MAX_DEPTH) return null
        if (!n.isVisibleToUser) return null
        budget[0]--
        val r = Rect().also(n::getBoundsInScreen)
        val kids = mutableListOf<GNode>()
        for (i in 0 until n.childCount) {
            val c = n.getChild(i) ?: continue
            try { node(c, budget, depth + 1, service)?.let(kids::add) } finally { @Suppress("DEPRECATION") c.recycle() }
        }
        val acceptsText = n.isEditable || n.actionList.any {
            it.id == AccessibilityNodeInfo.ACTION_SET_TEXT || it.id == AccessibilityNodeInfo.ACTION_PASTE
        }
        return GNode(
            text = n.text?.toString().orEmpty(), desc = n.contentDescription?.toString().orEmpty(),
            id = n.viewIdResourceName.orEmpty(), cls = n.className?.toString().orEmpty(),
            bounds = intArrayOf(r.left, r.top, r.right, r.bottom), clickable = n.isClickable,
            longClickable = n.isLongClickable, editable = acceptsText, password = n.isPassword,
            hint = n.hintText?.toString().orEmpty(), checked = if (n.isCheckable) n.isChecked else null,
            focused = n.isFocused && (acceptsText || n.isPassword), children = kids,
        )
    }

    @Suppress("DEPRECATION")
    fun resolve(pm: PackageManager, url: String): Pair<String, List<String>> = runCatching {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
        val chosen = pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY)?.activityInfo?.packageName
        val candidates = pm.queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY).map { it.activityInfo.packageName }.distinct()
        (chosen?.takeIf { it != "android" }.orEmpty()) to candidates
    }.getOrDefault("" to emptyList())

    @Suppress("DEPRECATION")
    fun labels(pm: PackageManager, pkgs: Collection<String>): Map<String, String> = pkgs.associateWith { p ->
        runCatching { pm.getApplicationLabel(pm.getApplicationInfo(p, 0)).toString() }.getOrDefault("")
    }.filterValues { it.isNotEmpty() }
}
