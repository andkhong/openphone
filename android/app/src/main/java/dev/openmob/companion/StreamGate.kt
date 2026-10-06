package dev.openmob.companion

/**
 * The live view never shows a blocked app (review B1). Every encoded video packet is held back [holdMs] before
 * it goes out. While a blocked (or unidentified) app is in any window, packets are dropped; when one appears,
 * everything still held (frames encoded since just before it was noticed) is dropped too. After it's gone, the
 * stream restarts only from a fresh keyframe, so no frame depends on a dropped one. Config packets (SPS/PPS)
 * carry no picture and always pass.
 *
 * Plain Kotlin (no Android), so the JVM tests cover it; ScreenStreamer feeds it from its encoder thread.
 */
class StreamGate<T>(private val holdMs: Long = 250) {
    private class Item<T>(val at: Long, val key: Boolean, val packet: T)

    private val held = ArrayDeque<Item<T>>()
    /** True after a protected screen: drop until a keyframe, and the caller asks the encoder for one. */
    var needKey = false
        private set

    /** A new packet. Returns what may be sent right now (config packets; everything else waits [holdMs]). */
    fun offer(now: Long, packet: T, key: Boolean, config: Boolean, protectedNow: Boolean): List<T> {
        if (config) return listOf(packet)
        if (protectedNow) return blackout()
        if (needKey) {
            if (!key) return flush(now, false)
            needKey = false
        }
        held.addLast(Item(now, key, packet))
        return flush(now, false)
    }

    /** Packets held long enough to send (none while protected: all held ones are dropped). */
    fun flush(now: Long, protectedNow: Boolean): List<T> {
        if (protectedNow) return blackout()
        val out = mutableListOf<T>()
        while (held.isNotEmpty() && now - held.first().at >= holdMs) out.add(held.removeFirst().packet)
        return out
    }

    private fun blackout(): List<T> {
        held.clear()
        needKey = true
        return emptyList()
    }
}
