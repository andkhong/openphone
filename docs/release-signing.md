# Release signing

The phone app comes in three builds. All have the same package (`dev.openmob.companion`).

| Build | Signed with | Debuggable | Plain `http://` | For |
|---|---|---|---|---|
| `debug` | Android's shared debug key | yes | local addresses | emulators and tests (`om build <env>`) |
| `local` | **the owner's release key** | no | local addresses (`ServerPolicy`) | the owner's own phone with their own server (`om build <env> signed`) |
| `release` | **the owner's release key** | no | never | what the hosted server offers for download (`om release`) |

`local` and `release` share the key, so a phone can move between them without reinstalling. A phone with a `debug` install has to uninstall OpenMob once (and pair again) to switch to a signed build. Android refuses to update an app with a different signature.

## Why

The `debug` build is debuggable. Anyone with USB access to the phone can read the app's private data, including its device key, with `run-as`. It's also signed with a key every Android developer machine has, so anyone could sign a "newer" app that installs over it. Security review 2026-10-09, M1.

## The key (once)

Run in your own terminal:

```sh
scripts/om keygen
```

It asks for a password (12 or more characters) without echoing it. It writes:
- `~/.config/openmob-envs/release.jks`: a 4096-bit RSA key, valid for 10,000 days;
- `~/.config/openmob-envs/release-signing.properties`: the paths and password Gradle reads.

Both are readable by you only (mode 600), and both are outside the repo. Set `OPENMOB_SIGNING` to keep the properties file elsewhere.

**Back up both files and the password,** for example in a password manager. If you lose them, every phone must uninstall OpenMob and pair again before it can take updates.

Agents never read these files or print the password. The build reads them itself.

## Building

```sh
scripts/om build gatekeeper signed   # the owner's phone: local build, installs with om phone gatekeeper
scripts/om release                   # main → deploy/OpenMob.apk (release), then: fly deploy -a openmob
```

To check a build, run `apksigner verify --print-certs <apk>`: it shows the signer. Then run `aapt2 dump xmltree <apk> --file AndroidManifest.xml`. It must show no `debuggable` attribute, and `usesCleartextTraffic=false` for `release`.
