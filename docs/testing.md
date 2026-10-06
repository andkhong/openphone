# Testing without a phone

OpenMob has **no sandbox mode** yet: there are no test keys, no simulated phones on the server, and
no `--demo` flag. Every task runs on a real Android device paired to a real account, and uses real
credits (and, on your own server, real model-provider usage). This page gives the best options
today, from closest-to-production to cheapest.

## An Android emulator

An emulator is a real Android device as far as OpenMob is concerned, so everything works the same:
pairing, accessibility, locks, approvals.

1. In Android Studio's Device Manager, create a virtual device with **Android 11 (API 30) or
   newer** and a Google Play or Google APIs system image.
2. In the emulator, set a screen lock (Settings → Security → Screen lock → PIN). OpenMob only pairs
   phones with a screen lock, and approvals need it.
3. Follow [Set up a phone](phone-setup.md). The emulator can't scan your QR code, so open the
   address shown under "Can't scan?" on the Connect page in the emulator's browser instead. You can
   also install the APK with `adb install OpenMob.apk`.
4. Keep the emulator unlocked while you test (a locked one gets `409 phone_locked`).

From a source checkout of OpenMob there is a second route that skips the app: `openmob bridge`
connects a phone attached over adb, and emulators count, to a server in accounts mode.

```sh
openmob bridge https://<server> --email you@example.com   # prompts for your password, then stays connected
```

The bridge has to keep running on the computer with the emulator.

## A spare phone

An old Android 11+ phone, paired to a separate test account, is the most realistic setup. Give it a
long screen timeout and turn off battery optimisation for OpenMob (see
[Troubleshooting](phone-setup.md#troubleshooting)).

## Keep tests cheap and harmless

- Use a separate API key for tests, with only the scopes they need, so you can revoke it alone.
- Use the `fast` tier (1 credit a step) and a small `max_steps`, such as 5.
- Prefer read-only instructions ("open Settings and read the Android version") over ones that buy,
  send or delete. Risky taps still stop for approval, but don't rely on that in tests.
- Pass `"memory": "off"` so test tasks neither read nor create memory notes.
- Watch your use with [`GET /v1/limits`](api.md#get-v1limits) and the console's **Overview**.
- Webhooks need a public `https://` URL. To receive them on your laptop, expose your receiver
  through an HTTPS tunnel (for example `cloudflared tunnel --url http://localhost:8000`).

## Testing your own code paths

To test how your code handles each status and error without a phone, mock the HTTP layer with the
real response shapes from the [API reference](api.md) and [Errors & limits](errors.md): a task in
each status, `needs_input` with a `question`, `409 phone_locked`, `429` with `Retry-After` and
`resets_at`, and a webhook body signed with a secret of your own (the signature scheme is in
[Webhooks](webhooks.md#verify-the-signature)).

## For contributors to OpenMob

The repository's test suite runs a real server against simulated phones: a fake phone that speaks
the app's protocol (`FakePhone` in `tests/test_remote.py`), a fake device (`FakeDriver` in
`tests/conftest.py`) and a scripted model client in place of an AI provider. They're internal test
helpers, not a supported API, but from a source checkout you can use them to run a local server with
fake phones for experiments. `pytest tests/test_api_v1.py` shows how they fit together.

### Gatekeeper suite on an emulator (evil server)

`tests/adversarial/evil_server.py` plays a compromised server against the real app on an emulator, and
plays the owner over adb (it approves and denies the phone's prompts and types the PIN). It covers
unknown methods, OpenMob and installer launches, dangerous links and typed text, ten security-critical
Settings pages and Build number, a bank app while it loads and once open (taps never reach it, reads
are black or empty, no live-view frames), password and OTP fields, a keyboard/autofill tap over a
WebView form, runtime permission dialogs, the phone-owned approval for "Buy now", that any other
action voids an approval, the purchase follow-up, a deep WebView "Place order", prompt flooding,
keep-awake by polling, and the friction of an honest checkout. The last results are in
[gatekeeper-b7-results.md](research/gatekeeper-b7-results.md).

Setup, on an Apple Silicon Mac (other hosts: the matching system image):

```sh
sdkmanager emulator "system-images;android-35;google_apis;arm64-v8a"
avdmanager create avd -n gk_b7 -k "system-images;android-35;google_apis;arm64-v8a" -d pixel_6
emulator -avd gk_b7 -no-window -no-audio -no-snapshot-save -gpu swiftshader_indirect &
(cd android && ./gradlew assembleDebug)
android/gradlew -p tests/adversarial/standins assembleDebug   # com.example.bank, .shop, .login
adb -s emulator-5554 install -r android/app/build/outputs/apk/debug/app-debug.apk   # and the three stand-ins
adb -s emulator-5554 shell locksettings set-pin 1234
adb -s emulator-5554 shell settings put secure enabled_accessibility_services \
    dev.openmob.companion/.OpenMobAccessibilityService
```

Pair the app once with **Use a developer token instead**: server `http://10.0.2.2:8911`, any token
(say `b7evil`). Then run:

```sh
python tests/adversarial/evil_server.py --serial emulator-5554 --port 8911 --token b7evil \
    --pin 1234 --restart --out /tmp/evil-evidence
```

`--groups` picks some of the groups; `--restart` restarts the app before each group (fresh rate
limits and denial counts) and presses Connect. Evidence (screenshots, the prompts' layouts, the live
view's raw H.264, `results.json`) goes to `--out`. Every adb call names the device with `--serial`:
never run it against your own phone. Two harness details: the gate prompt is hidden from accessibility
services and screenshots, so the suite finds its buttons with `dumpsys activity`; and `uiautomator dump`
unbinds OpenMob's accessibility service (the suite turns it back on), so it's used only where no
approval is about to be re-sent.
