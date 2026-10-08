package dev.openmob.companion

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Blocking HTTP calls to the OpenMob server's pairing endpoints. Call only from a background
 * thread. Failures throw [ApiError] with a message fit to show the user.
 *
 * Pairing never leaves a dashboard session on the phone: a pairing code is the credential
 * ([check], then [claim] after the owner confirms), and the phone keeps only its own device key.
 * The sign-in fallback ([signIn] → [mintCode]) uses a session for a few seconds, in memory, and
 * signs it out again ([logout]) before the owner is even asked to confirm.
 */
class AccountApi(server: String) {
    class ApiError(message: String, val status: Int = 0) : Exception(message)

    /** Who a pairing code would pair this phone with, shown in the screen-lock prompt. */
    data class CodeInfo(val email: String)
    data class Claimed(val key: String, val email: String)

    private val base = server.trim().trimEnd('/')

    fun check(code: String): CodeInfo {
        val body = call(post("/api/pair/check", JSONObject().put("code", code)))
        return CodeInfo(body.optString("email").ifBlank { throw ApiError("The server didn't say which account this is.") })
    }

    /** Redeem the code for this phone's own device key. No session: the code is the credential. */
    fun claim(code: String, deviceId: String, model: String, approvalKey: String?): Claimed {
        val json = JSONObject().put("code", code).put("device_id", deviceId).put("model", model)
            .apply { if (approvalKey != null) put("approval_key", approvalKey) }
        val body = call(post("/api/pair/claim", json))
        val key = body.optString("key").ifBlank { throw ApiError("The server did not return a device key.") }
        return Claimed(key, body.optString("email"))
    }

    /** Fallback without a laptop: sign in, returning a session that the caller keeps in memory only. */
    fun signIn(email: String, password: String, create: Boolean, invite: String = ""): String {
        val creds = JSONObject().put("email", email).put("password", password)
        // Sign-up needs a one-time invite code; the reply is the same whether or not the email was new.
        if (create) call(post("/api/auth/signup", JSONObject(creds.toString()).put("invite", invite)))
        val body = call(post("/api/auth/login", creds))
        return body.optString("session").ifBlank { throw ApiError("The server did not return a session.") }
    }

    /** A pairing code for this account, made with a just-signed-in session. */
    fun mintCode(session: String): String {
        val body = call(post("/api/pairing", JSONObject(), session))
        if (!body.optBoolean("accounts", false)) throw ApiError("This server doesn't use accounts.")
        return body.optString("code").ifBlank { throw ApiError("The server did not return a pairing code.") }
    }

    /** Best effort: end a session on the server. */
    fun logout(session: String) = quietly(post("/api/auth/logout", JSONObject(), session))

    /** Best effort: revoke this phone's pairing, proven with its own device key. */
    fun unpair(deviceId: String, deviceKey: String) = quietly(
        Request.Builder().url("$base/api/phone/unpair").header(DEVICE_KEY_HEADER, deviceKey)
            .post(JSONObject().put("device_id", deviceId).toBody()).build()
    )

    private fun post(path: String, json: JSONObject, session: String? = null): Request =
        Request.Builder().url(base + path).post(json.toBody())
            .apply { if (session != null) header(SESSION_HEADER, session) }.build()

    private fun quietly(request: Request) {
        try {
            client.newCall(request).execute().close()
        } catch (_: Exception) {
        }
    }

    private fun call(request: Request): JSONObject {
        val response = try {
            client.newCall(request).execute()
        } catch (e: IOException) {
            throw ApiError("Couldn't reach the server: ${e.message ?: e.javaClass.simpleName}")
        }
        response.use {
            val text = it.body?.string().orEmpty()
            val json = try { JSONObject(text) } catch (_: Exception) { null }
            if (!it.isSuccessful) {
                // FastAPI validation errors (422) carry a list here, not a sentence; fall back.
                val detail = (json?.opt("detail") as? String).orEmpty().ifBlank { "Server error (HTTP ${it.code})." }
                throw ApiError(detail, it.code)
            }
            return json ?: throw ApiError("Unexpected reply from the server.")
        }
    }

    private fun JSONObject.toBody() = toString().toRequestBody(JSON)

    companion object {
        const val SESSION_HEADER = "X-OpenMob-Session"
        const val DEVICE_KEY_HEADER = "X-OpenMob-Device-Key"
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private val client = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .build()
    }
}
