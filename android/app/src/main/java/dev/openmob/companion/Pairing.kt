package dev.openmob.companion

import java.net.URI
import java.net.URLDecoder

/**
 * Pure pairing rules (no Android types), unit-tested in PairingTest.
 *
 * A pairing link is `openmob://connect?server=<https://host>&code=<one-time code>`, from the
 * dashboard's QR code (via the /pair page) or from `openmob phone add` over USB. A link never
 * changes anything by itself: the owner confirms the host and account with the screen lock first.
 */
object Pairing {
    sealed class Link {
        data class Account(val server: String, val code: String) : Link()
        /** Developer-token links are refused: a web page could otherwise hijack the phone. */
        object TokenRefused : Link()
        object Invalid : Link()
    }

    private val CODE = Regex("^[A-Za-z0-9_-]{16,128}$")

    fun parse(url: String?): Link? {
        val uri = try { URI(url ?: return null) } catch (e: Exception) { return null }
        if (uri.scheme?.lowercase() != "openmob" || uri.host?.lowercase() != "connect") return null
        val query = (uri.rawQuery ?: "").split("&").filter { it.isNotEmpty() }.associate { part ->
            val (k, v) = (part.split("=", limit = 2) + "").let { it[0] to it[1] }
            decode(k) to decode(v)
        }
        val server = query["server"]?.trim().orEmpty().trimEnd('/')
        val code = query["code"]?.trim().orEmpty()
        if (code.isEmpty()) {
            return if (!(query["t"] ?: query["token"]).isNullOrBlank()) Link.TokenRefused else Link.Invalid
        }
        if (!CODE.matches(code) || !ServerPolicy.isAllowed(server)) return Link.Invalid
        return Link.Account(server, code)
    }

    private fun decode(s: String): String = try { URLDecoder.decode(s, "UTF-8") } catch (e: Exception) { "" }

    /** Which battery screen to send the owner to (see MainActivity.onBatteryClicked). */
    enum class BatteryGuide { SAMSUNG, STANDARD }

    fun batteryGuide(manufacturer: String?): BatteryGuide =
        if (manufacturer.orEmpty().trim().equals("samsung", ignoreCase = true)) BatteryGuide.SAMSUNG else BatteryGuide.STANDARD
}
