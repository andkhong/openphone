package dev.openmob.companion.gate

import java.text.Normalizer

/**
 * Sensitive numbers: payment card numbers and US Social Security numbers (docs/safety.md, "Sensitive numbers";
 * findings F2 and F3 of the 2026-10-10 abuse series). Mirrors openmob/agent/sensitive.py and tests/gatekeeper_ref.py;
 * the shared fixture tests/fixtures/gatekeeper/sensitive_numbers.json pins all three to the same answers.
 *
 * - A card number: 13-19 digits, optionally grouped by spaces (one or more), dashes, pluses, dots, slashes or
 *   underscores (a grouped number starts with a 4-digit group), first digit 2-6, passing the Luhn check; not after "+"
 *   (a phone number) or "<digit>." (a decimal). Any Unicode decimal digit counts as its ASCII digit.
 * - An SSN: ddd-dd-dddd, ddd dd dddd or ddd.dd.dddd with a valid area (not 000, 666, 9xx), group (not 00) and serial
 *   (not 0000). Where the field or screen says SSN or Social Security (rules.json sensitive_numbers.ssn_label), also 9
 *   bare digits and test values like 000-12-3456.
 * - In a link, also after percent-decoding until it stops changing (at most 10 rounds), and a card number split over
 *   consecutive numeric query parameters (each 3-12 digits, no decimals).
 *
 * A guard against accidental leaks, not against a determined adversary (base64, words or several requests aren't
 * caught; review L1, 2026-10-17).
 */
object SensitiveNumbers {
    const val CARD = "card"
    const val SSN = "ssn"

    private val RUN = Regex("(?<![0-9+])(?<![0-9]\\.)[0-9](?:(?: +|[+\\-./_])?[0-9])*")
    private val SEP = Regex(" +|[+\\-./_]")
    private val SSN_RX = Regex("(?<![0-9\\-.])([0-9]{3})([ \\-.]?)([0-9]{2})\\2([0-9]{4})(?![0-9]|[\\-./][0-9])")
    private val QUERY = Regex("[?&;]([^=&;#]*)=([^&;#]*)")
    private val NUMERIC = Regex("^[0-9][0-9 +\\-_]*$") // no dots: a decimal (a coordinate) is not a piece
    private val DIGIT = Regex("[0-9]")

    fun luhn(digits: String): Boolean {
        var total = 0
        for ((i, ch) in digits.reversed().withIndex()) {
            var d = ch - '0'
            if (i % 2 == 1) {
                d *= 2
                if (d > 9) d -= 9
            }
            total += d
        }
        return total % 10 == 0
    }

    /** Every Unicode decimal digit (Arabic-Indic, Devanagari, ...) as its ASCII digit; one code point for one. */
    fun asciiDigits(text: String): String {
        if (text.all { it.code < 128 }) return text
        val out = StringBuilder()
        var i = 0
        while (i < text.length) {
            val cp = text.codePointAt(i)
            if (Character.getType(cp) == Character.DECIMAL_DIGIT_NUMBER.toInt()) out.append('0' + Character.digit(cp, 10))
            else out.appendCodePoint(cp)
            i += Character.charCount(cp)
        }
        return out.toString()
    }

    private fun norm(text: String?): String = asciiDigits(Normalizer.normalize(text ?: "", Normalizer.Form.NFKC))

    fun hasCard(text: String): Boolean {
        for (m in RUN.findAll(text)) {
            val groups = SEP.split(m.value)
            var total = 0
            for ((k, g) in groups.withIndex()) {
                total += g.length
                if (total > 19 || (k > 0 && groups[0].length != 4)) break
                if (total < 13) continue
                val digits = groups.subList(0, k + 1).joinToString("")
                if (digits[0] in "23456" && luhn(digits)) return true
                break
            }
        }
        return false
    }

    fun hasSsn(text: String, labeled: Boolean): Boolean = SSN_RX.findAll(text).any { m ->
        val (area, sep, group, serial) = m.destructured
        labeled || (sep.isNotEmpty() && area != "000" && area != "666" && area[0] != '9' && group != "00" && serial != "0000")
    }

    /** CARD or SSN when [text] holds a sensitive number, else null. */
    fun find(text: String?, ssnContext: Boolean = false, cardField: Boolean = false): String? {
        val t = norm(text)
        if (hasCard(t)) return CARD
        if (hasSsn(t, ssnContext)) return SSN
        if (cardField && DIGIT.findAll(t).count() >= 3) return CARD
        return null
    }

    /** Percent-decoding as Python's urllib.parse.unquote does it: valid %XX runs become UTF-8, the rest stays. */
    fun unquote(s: String): String {
        if ('%' !in s) return s
        val out = StringBuilder()
        var i = 0
        while (i < s.length) {
            if (s[i] == '%' && i + 2 < s.length && hex(s[i + 1]) >= 0 && hex(s[i + 2]) >= 0) {
                val bytes = java.io.ByteArrayOutputStream()
                while (i + 2 < s.length && s[i] == '%' && hex(s[i + 1]) >= 0 && hex(s[i + 2]) >= 0) {
                    bytes.write(hex(s[i + 1]) * 16 + hex(s[i + 2]))
                    i += 3
                }
                out.append(String(bytes.toByteArray(), Charsets.UTF_8))
            } else {
                out.append(s[i])
                i++
            }
        }
        return out.toString()
    }

    private fun hex(c: Char): Int = when (c) {
        in '0'..'9' -> c - '0'
        in 'a'..'f' -> c - 'a' + 10
        in 'A'..'F' -> c - 'A' + 10
        else -> -1
    }

    /** A URL percent-decoded until it stops changing (at most 10 rounds), NFKC-normalized. */
    fun decodeUrl(url: String?): String {
        var text = url ?: ""
        repeat(10) {
            val decoded = unquote(text)
            if (decoded == text) return norm(text)
            text = decoded
        }
        return norm(text)
    }

    /** CARD or SSN when a link carries a sensitive number anywhere (host, path, query or fragment, decoded). */
    fun inUrl(url: String?, ssnLabel: Regex): String? {
        val text = decodeUrl(url)
        val found = find(text, ssnContext = ssnLabel.containsMatchIn(text))
        return if (found == null && splitCard(text)) CARD else found
    }

    /** A card number split over consecutive numeric query parameters (`?a=4111111&b=111111111`): two or more values in
     *  a row, each only digits (spaces, dashes, underscores or pluses between) with 3-12 digits, whose digits together are a
     *  card number. */
    fun splitCard(text: String): Boolean {
        val pieces = QUERY.findAll(text).map { m ->
            val v = m.groupValues[2].trim()
            val digits = v.filter { it in '0'..'9' }
            if (NUMERIC.matches(v) && digits.length in 3..12) digits else null
        }.toList()
        for (i in pieces.indices) {
            var joined = pieces[i] ?: continue
            for (j in i + 1 until pieces.size) {
                val next = pieces[j] ?: break
                joined += next
                if (joined.length > 19) break
                if (joined.length >= 13 && joined[0] in "23456" && luhn(joined)) return true
            }
        }
        return false
    }

    fun describe(kind: String?): String = when (kind) {
        CARD -> "a card number"
        SSN -> "a Social Security number"
        else -> "a sensitive number"
    }
}
