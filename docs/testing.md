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
