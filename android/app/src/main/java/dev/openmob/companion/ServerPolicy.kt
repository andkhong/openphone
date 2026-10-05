package dev.openmob.companion

import java.net.URI

/**
 * Which server addresses the app may pair with or connect to. `https://` anywhere; plain
 * `http://` only on this phone or the local network (loopback, private ranges, link-local,
 * `.local`), where a developer runs their own server. Checked when a link or the pair form is
 * used, and again before every connection, so a stored bad URL can't be used either.
 */
object ServerPolicy {
    fun isAllowed(url: String): Boolean {
        val uri = try { URI(url.trim()) } catch (e: Exception) { return false }
        val host = uri.host?.trim('[', ']')?.lowercase().orEmpty()
        if (host.isEmpty() || uri.userInfo != null) return false
        return when (uri.scheme?.lowercase()) {
            "https" -> true
            "http" -> isLocal(host)
            else -> false
        }
    }

    fun hostOf(url: String): String = try { URI(url.trim()).host ?: url } catch (e: Exception) { url }

    fun isLocal(host: String): Boolean {
        if (host == "localhost" || host.endsWith(".local")) return true
        val v4 = host.split(".").mapNotNull { it.toIntOrNull()?.takeIf { n -> n in 0..255 } }
        if (v4.size == 4 && host.count { it == '.' } == 3) {
            val (a, b) = v4[0] to v4[1]
            return a == 127 || a == 10 || (a == 172 && b in 16..31) || (a == 192 && b == 168) || (a == 169 && b == 254)
        }
        if (':' in host) { // IPv6: loopback, link-local fe80::/10, unique-local fc00::/7
            return host == "::1" || host.startsWith("fe8") || host.startsWith("fe9") || host.startsWith("fea") ||
                host.startsWith("feb") || host.startsWith("fc") || host.startsWith("fd")
        }
        return false
    }
}
