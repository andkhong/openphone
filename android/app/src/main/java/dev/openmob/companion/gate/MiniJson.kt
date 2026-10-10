package dev.openmob.companion.gate

/**
 * A small JSON reader for the gatekeeper's rule file and fixtures. The engine is plain Kotlin (no Android
 * classes) so it runs unchanged in JVM unit tests, where org.json is only a stub. Objects become
 * LinkedHashMap<String, Any?>, arrays List<Any?>, numbers Long or Double.
 */
object MiniJson {
    fun parse(text: String): Any? = Reader(text).run { val v = value(); skip(); require(i == s.length) { "trailing data" }; v }

    private class Reader(val s: String) {
        var i = 0

        fun skip() { while (i < s.length && s[i].isWhitespace()) i++ }

        fun value(): Any? {
            skip()
            require(i < s.length) { "unexpected end" }
            return when (val c = s[i]) {
                '{' -> obj()
                '[' -> arr()
                '"' -> str()
                't' -> lit("true", true)
                'f' -> lit("false", false)
                'n' -> lit("null", null)
                else -> if (c == '-' || c.isDigit()) num() else throw IllegalArgumentException("bad json at $i")
            }
        }

        fun lit(word: String, v: Any?): Any? {
            require(s.startsWith(word, i)) { "bad literal at $i" }
            i += word.length
            return v
        }

        fun obj(): Map<String, Any?> {
            val out = LinkedHashMap<String, Any?>()
            i++
            skip()
            if (s[i] == '}') { i++; return out }
            while (true) {
                skip()
                val k = str()
                skip()
                require(s[i] == ':') { "expected : at $i" }
                i++
                out[k] = value()
                skip()
                when (s[i++]) {
                    ',' -> continue
                    '}' -> return out
                    else -> throw IllegalArgumentException("expected , or } at ${i - 1}")
                }
            }
        }

        fun arr(): List<Any?> {
            val out = ArrayList<Any?>()
            i++
            skip()
            if (s[i] == ']') { i++; return out }
            while (true) {
                out.add(value())
                skip()
                when (s[i++]) {
                    ',' -> continue
                    ']' -> return out
                    else -> throw IllegalArgumentException("expected , or ] at ${i - 1}")
                }
            }
        }

        fun str(): String {
            require(s[i] == '"') { "expected string at $i" }
            i++
            val b = StringBuilder()
            while (true) {
                val c = s[i++]
                when (c) {
                    '"' -> return b.toString()
                    '\\' -> when (val e = s[i++]) {
                        'n' -> b.append('\n'); 't' -> b.append('\t'); 'r' -> b.append('\r')
                        'b' -> b.append('\b'); 'f' -> b.append('\u000c')
                        'u' -> { b.append(s.substring(i, i + 4).toInt(16).toChar()); i += 4 }
                        else -> b.append(e)
                    }
                    else -> b.append(c)
                }
            }
        }

        fun num(): Any {
            val start = i
            if (s[i] == '-') i++
            while (i < s.length && (s[i].isDigit() || s[i] in ".eE+-")) i++
            val t = s.substring(start, i)
            return if (t.any { it in ".eE" }) t.toDouble() else t.toLong()
        }
    }
}

@Suppress("UNCHECKED_CAST")
internal fun Any?.obj(): Map<String, Any?> = this as? Map<String, Any?> ?: emptyMap()

internal fun Any?.list(): List<Any?> = this as? List<Any?> ?: emptyList()

internal fun Any?.strings(): List<String> = list().map { it.toString() }

internal fun Map<String, Any?>.str(key: String, default: String = ""): String = (this[key] as? String) ?: default

internal fun Map<String, Any?>.long(key: String, default: Long = 0): Long = when (val v = this[key]) {
    is Long -> v
    is Double -> v.toLong()
    is Int -> v.toLong()
    else -> default
}

internal fun Map<String, Any?>.dbl(key: String, default: Double): Double = when (val v = this[key]) {
    is Number -> v.toDouble()
    is String -> v.toDoubleOrNull() ?: default
    else -> default
}

internal fun Map<String, Any?>.bool(key: String): Boolean = this[key] == true
