package dev.openmob.companion.gate

/**
 * Reads one window's tree into [GNode]s for the gatekeeper (review B2). The context the rules need (prices,
 * payment fields, the page title, language) is read within a node budget and a depth limit; but every node that
 * contains a point the call touches is always read, children and all, with no budget, so the hit test sees the
 * real target however deep a WebView checkout nests it. If a branch under a point can't be read to the end
 * (a hard depth cap, or a child that vanished mid-read), its last node is marked `truncated` and the gate asks.
 *
 * Generic over the node type so the JVM tests can run it on fake trees; GateScreens adapts AccessibilityNodeInfo.
 */
class TreeWalk<N>(private val adapter: Adapter<N>, private val points: List<Pair<Double, Double>>,
                  private val budget: Int = 1500, private val maxDepth: Int = 60, private val hardDepth: Int = 400) {
    interface Adapter<N> {
        fun bounds(n: N): IntArray
        fun visible(n: N): Boolean
        fun childCount(n: N): Int
        /** null: the child couldn't be read (it vanished). */
        fun child(n: N, i: Int): N?
        fun release(n: N) {}
        fun leaf(n: N, children: List<GNode>, truncated: Boolean): GNode
    }

    private var left = budget

    private fun underPoint(b: IntArray) = points.any { (x, y) -> b[0] <= x && x < b[2] && b[1] <= y && y < b[3] }

    fun read(root: N): GNode? = node(root, 0)

    private fun node(n: N, depth: Int): GNode? {
        if (!adapter.visible(n)) return null
        val b = adapter.bounds(n)
        val pinned = underPoint(b)
        if (!pinned && (left <= 0 || depth > maxDepth)) return null
        left--
        val kids = mutableListOf<GNode>()
        var truncated = false
        val count = adapter.childCount(n)
        if (depth >= hardDepth && count > 0) {
            truncated = true
        } else {
            for (i in 0 until count) {
                val c = adapter.child(n, i)
                if (c == null) {
                    if (pinned) truncated = true // something under the point couldn't be read
                    continue
                }
                try {
                    node(c, depth + 1)?.let(kids::add)
                } finally {
                    adapter.release(c)
                }
            }
        }
        return adapter.leaf(n, kids, truncated && pinned)
    }
}
