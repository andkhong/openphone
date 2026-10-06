package dev.openmob.companion

/**
 * What a BiometricPrompt error means for a gate prompt (plain Kotlin, tested in the JVM). Only the owner's
 * explicit no is a denial: cancelling the prompt themselves ("Cancel", the back gesture). Everything else is the
 * system interrupting (a heads-up notification or call taking focus, the screen turning off, the sensor timing
 * out, too many attempts): the request stays pending and the prompt comes back; if nobody answers within the
 * prompt's 2 minutes it ends as "no answer", never as "denied".
 */
object PromptOutcome {
    // android.hardware.biometrics.BiometricPrompt error codes.
    const val ERROR_TIMEOUT = 3
    const val ERROR_CANCELED = 5
    const val ERROR_LOCKOUT = 7
    const val ERROR_LOCKOUT_PERMANENT = 9
    const val ERROR_USER_CANCELED = 10
    const val ERROR_NEGATIVE_BUTTON = 13

    enum class Kind { DECLINED, INTERRUPTED }

    fun classify(errorCode: Int): Kind = when (errorCode) {
        ERROR_USER_CANCELED, ERROR_NEGATIVE_BUTTON -> Kind.DECLINED
        else -> Kind.INTERRUPTED
    }
}
