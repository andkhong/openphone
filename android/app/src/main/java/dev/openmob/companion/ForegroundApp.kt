package dev.openmob.companion

/** Which app is in front, for current_app. The active window's own package is the truth. When that window
 *  can't be read (Android hides some pages from accessibility, e.g. Developer options), the app remembered
 *  from window events is used only if no window has changed since it was noted: otherwise it may be the
 *  previous app (seen on a Samsung: Gallery reported while Settings was in front), and unknown is honest. */
internal object ForegroundApp {
    fun pick(fromWindow: String?, remembered: String?, rememberedAt: Long, windowChangeAt: Long,
             ignored: Set<String>): String? {
        if (fromWindow != null && fromWindow !in ignored) return fromWindow
        return remembered?.takeIf { it !in ignored && rememberedAt >= windowChangeAt }
    }
}
