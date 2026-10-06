package dev.openmob.companion

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * The key that proves an approval came through this phone's screen lock (docs/phone-protocol.md).
 * An EC P-256 pair in the Android Keystore that can only sign right after the owner passes a strong
 * biometric or the device credential. Its public half is registered with the server at pairing (or
 * on first connect); approvals carry a signature over `<approval id>|<task id>|true|<unix time>`,
 * so a stolen device key, or anything else on the socket, can't approve. Denials aren't signed.
 */
object ApprovalKeys {
    private const val ALIAS = "openmob_approval"

    private fun store(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    /** The public key (base64 X.509 SubjectPublicKeyInfo), creating the pair the first time. */
    fun publicKey(): String? = try {
        val ks = store()
        if (!ks.containsAlias(ALIAS)) create()
        Base64.encodeToString(ks.getCertificate(ALIAS).publicKey.encoded, Base64.NO_WRAP)
    } catch (e: Exception) {
        null // no Keystore support: the server treats this phone like an older app
    }

    private fun create() {
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
        generator.initialize(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(true)
                // Every signature needs a fresh screen-lock check (timeout 0: per use, via BiometricPrompt).
                .setUserAuthenticationParameters(
                    0, KeyProperties.AUTH_BIOMETRIC_STRONG or KeyProperties.AUTH_DEVICE_CREDENTIAL,
                )
                .build(),
        )
        generator.generateKeyPair()
    }

    /** A Signature ready to hand to BiometricPrompt as its CryptoObject, or null without a key. */
    fun signer(): Signature? = try {
        val key = store().getKey(ALIAS, null) as? PrivateKey ?: return null
        Signature.getInstance("SHA256withECDSA").apply { initSign(key) }
    } catch (e: Exception) {
        null // e.g. the key was invalidated by removing the screen lock: pair again to approve
    }

    /** `always`: "Always allow this" is signed too (`…|always`), so the grant carries the screen lock. */
    fun message(approvalId: String, taskId: String, timestamp: Long, always: Boolean = false): ByteArray =
        ("$approvalId|$taskId|true|$timestamp" + if (always) "|always" else "").toByteArray()

    /** Sign with a Signature the BiometricPrompt just unlocked. */
    fun sign(unlocked: Signature, approvalId: String, taskId: String, timestamp: Long, always: Boolean = false): String {
        unlocked.update(message(approvalId, taskId, timestamp, always))
        return Base64.encodeToString(unlocked.sign(), Base64.NO_WRAP)
    }
}
