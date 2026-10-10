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

    /** Just the packages showing in any window (for reads: is a blocked app visible anywhere?). A window whose
     *  root can't be read yet (an app still starting) is kept with an empty package: reads treat it as unknown. */
    fun windowPackages(service: OpenMobAccessibilityService): GScreen = GScreen(
        windows = runCatching { service.windows }.getOrDefault(emptyList()).map { w ->
            val root = w.root
            val pkg = root?.packageName?.toString().orEmpty()
            @Suppress("DEPRECATION") root?.recycle()
            GWindow(w.id, typeName(w.type), w.layer, pkg, w.title?.toString().orEmpty(), IntArray(4), null,
                service.windowSince[w.id] ?: 0L)
        },
        // A permission dialog Android hides is identified by its window-state event (reads may show it).
        eventPackage = service.lastEventPackage, eventText = service.lastEventText, eventAt = service.lastEventAt,
    )

    /** What the owner-dialog check needs, cheaply: every window with a shallow read of its tree, and the last
     *  window-state event. */
    fun dialogScreen(service: OpenMobAccessibilityService): GScreen = GScreen(
        windows = runCatching { service.windows }.getOrDefault(emptyList()).map { w ->
            val root = w.root
            val pkg = root?.packageName?.toString().orEmpty()
            val tree = root?.let { TreeWalk(ADAPTER, emptyList(), budget = 80, maxDepth = 12).read(it) }
            GWindow(w.id, typeName(w.type), w.layer, pkg, w.title?.toString().orEmpty(), IntArray(4), tree,
                service.windowSince[w.id] ?: 0L)
        },
        eventPackage = service.lastEventPackage, eventText = service.lastEventText, eventAt = service.lastEventAt,
    )

    /** [points]: where the call touches; nodes under them are always read in full (TreeWalk). */
    fun capture(service: OpenMobAccessibilityService, urls: List<String> = emptyList(),
                labelsFor: Collection<String> = emptyList(), points: List<Pair<Double, Double>> = emptyList()): GScreen {
        val windows = mutableListOf<GWindow>()
        // The live input focus (where typing and ENTER go) is pinned like a tap point, so its field and its
        // ancestors are always read, however deep (review R1). If it still isn't read, the gate refuses.
        val focus = runCatching { service.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) }.getOrNull()
        val focusPoint = focus?.let { f ->
            Rect().also(f::getBoundsInScreen).let { r -> r.exactCenterX().toDouble() to r.exactCenterY().toDouble() }
        }
        // The focused node itself, and its parent chain: a Jetpack Compose field (Google Messages) can be missing
        // from the walk from the root, but findFocus resolves it.
        val focusWindow = focus?.windowId
        val focusNode = focus?.let { runCatching { ADAPTER.leaf(it, emptyList(), false) }.getOrNull() }
        val focusAncestors = mutableListOf<GNode>()
        var up = runCatching { focus?.parent }.getOrNull()
        while (up != null && focusAncestors.size < 40) {
            runCatching { ADAPTER.leaf(up, emptyList(), false) }.getOrNull()?.let { focusAncestors.add(0, it) }
            val next = runCatching { up.parent }.getOrNull()
            @Suppress("DEPRECATION") up.recycle()
            up = next
        }
        @Suppress("DEPRECATION") focus?.recycle()
        val pinned = points + listOfNotNull(focusPoint)
        for (w in runCatching { service.windows }.getOrDefault(emptyList())) {
            val root = w.root
            val bounds = Rect().also(w::getBoundsInScreen)
            val pkg = root?.packageName?.toString().orEmpty()
            val tree = root?.let { TreeWalk(ADAPTER, pinned, budget = MAX_NODES, maxDepth = MAX_DEPTH).read(it) }
            windows.add(GWindow(w.id, typeName(w.type), w.layer, pkg, w.title?.toString().orEmpty(),
                intArrayOf(bounds.left, bounds.top, bounds.right, bounds.bottom), tree, service.windowSince[w.id] ?: 0L))
        }
        val keyguard = service.getSystemService(android.app.KeyguardManager::class.java)
        val pm = service.packageManager
        // An uninstall dialog or an app-info page names an app by its label: the launchable apps' labels let the gate
        // tell which (and refuse OpenMob or a blocked app).
        val naming = windows.any { "packageinstaller" in it.pkg || it.pkg == "com.android.settings" }
        val pkgs = (windows.map { it.pkg } + labelsFor + GateHost.lastAppOrEmpty() + (if (naming) launchable(pm) else emptyList()))
            .filter { it.isNotEmpty() }.toSet()
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
            // The owner's own names, for "self" recipients (the paired account's email; Note to self and You are built in).
            owner = listOfNotNull(dev.openmob.companion.Settings(service).email.takeIf { it.isNotEmpty() }),
            focusExpected = focusPoint != null,
            focusWindow = focusWindow, focusNode = focusNode, focusAncestors = focusAncestors,
            eventPackage = service.lastEventPackage, eventText = service.lastEventText, eventAt = service.lastEventAt,
        )
    }

    private val ADAPTER = object : TreeWalk.Adapter<AccessibilityNodeInfo> {
        override fun bounds(n: AccessibilityNodeInfo) = Rect().also(n::getBoundsInScreen).let { intArrayOf(it.left, it.top, it.right, it.bottom) }
        override fun visible(n: AccessibilityNodeInfo) = n.isVisibleToUser
        override fun childCount(n: AccessibilityNodeInfo) = n.childCount
        override fun child(n: AccessibilityNodeInfo, i: Int): AccessibilityNodeInfo? = n.getChild(i)
        @Suppress("DEPRECATION") override fun release(n: AccessibilityNodeInfo) = n.recycle()
        override fun leaf(n: AccessibilityNodeInfo, children: List<GNode>, truncated: Boolean): GNode {
            val acceptsText = n.isEditable || n.actionList.any {
                it.id == AccessibilityNodeInfo.ACTION_SET_TEXT || it.id == AccessibilityNodeInfo.ACTION_PASTE
            }
            return GNode(
                text = n.text?.toString().orEmpty(), desc = n.contentDescription?.toString().orEmpty(),
                id = n.viewIdResourceName.orEmpty(), cls = n.className?.toString().orEmpty(),
                bounds = bounds(n), clickable = n.isClickable,
                longClickable = n.isLongClickable, editable = acceptsText, password = n.isPassword,
                hint = n.hintText?.toString().orEmpty(), checked = if (n.isCheckable) n.isChecked else null,
                focused = n.isFocused && (acceptsText || n.isPassword), children = children, truncated = truncated,
            )
        }
    }

    @Suppress("DEPRECATION")
    fun resolve(pm: PackageManager, url: String): Pair<String, List<String>> = runCatching {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
        val chosen = pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY)?.activityInfo?.packageName
        val candidates = pm.queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY).map { it.activityInfo.packageName }.distinct()
        (chosen?.takeIf { it != "android" }.orEmpty()) to candidates
    }.getOrDefault("" to emptyList())

    @Suppress("DEPRECATION")
    private fun launchable(pm: PackageManager): List<String> = runCatching {
        pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
            .map { it.activityInfo.packageName }.distinct()
    }.getOrDefault(emptyList())

    fun labels(pm: PackageManager, pkgs: Collection<String>): Map<String, String> = pkgs.associateWith { p ->
        runCatching { pm.getApplicationLabel(pm.getApplicationInfo(p, 0)).toString() }.getOrDefault("")
    }.filterValues { it.isNotEmpty() }
}
