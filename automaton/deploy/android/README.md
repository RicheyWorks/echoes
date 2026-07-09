# automaton Android client

A Jetpack Compose app that talks to a remote automaton server over its
HTTP API. Three tabs: Runs, Workflows, Settings.

| | |
|---|---|
| Min Android | 8.0 (API 26) — ~94 % of active devices |
| Architecture | Jetpack Compose + Kotlin coroutines; OkHttp |
| Storage | EncryptedSharedPreferences (token), SharedPreferences (URL) |
| TLS | trusts system store OR pins a known self-signed cert by SHA-256 |
| Distribution | APK sideload (recommended for personal use) |

## Layout

```
deploy/android/
  build.gradle.kts               root build; plugin declarations
  settings.gradle.kts            module graph
  gradle/
    libs.versions.toml           version catalog
    wrapper/
      gradle-wrapper.properties  Gradle 8.6
  app/
    build.gradle.kts             app module; Compose BOM, OkHttp, security-crypto
    src/main/
      AndroidManifest.xml        activity + INTERNET permission
      res/xml/
        network_security_config.xml  allow HTTP on LAN/Tailscale ranges
      kotlin/com/automaton/
        MainActivity.kt          ComponentActivity → NavHost + bottom bar
        Settings.kt              EncryptedSharedPreferences credential store
        client/
          AutomatonClient.kt     OkHttp coroutine client + cert pinning
          Models.kt              @Serializable data classes
        screens/
          RunsListScreen.kt      LazyColumn, pull-to-refresh, status badges
          RunDetailScreen.kt     step tree + 2 s polling + SignalResponder
          WorkflowsScreen.kt     workflow list + TriggerSheet
          SettingsScreen.kt      URL + token + cert pin + health test
```

## Build + run

Requires **Android Studio Hedgehog** (2023.1.1) or later, or a JDK 17+
install with the Android SDK.

```bash
cd deploy/android
./gradlew assembleDebug          # builds app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug           # builds + installs on a connected device / emulator
```

Or in Android Studio:

1. **File → Open** → pick `deploy/android/`.
2. Wait for Gradle sync to finish (~1 min first time).
3. Pick an emulator or USB-connected device → press **▶ Run**.

### First-run setup

1. Open the **Settings** tab (gear icon).
2. Enter your server URL — e.g.
   `https://automaton.your-tailnet.ts.net:8443` (Tailscale)
   or `https://192.168.x.y:8443` (LAN).
3. Paste your `AUTOMATON_TOKEN`.
4. If you used `automaton tls init` (self-signed cert) and don't want to
   install it as a trusted CA on the device, paste the cert's SHA-256
   fingerprint into "self-signed cert pin".
   `automaton tls init` prints the fingerprint after generation.
5. Tap **Test connection** — you should see "Connection OK".

The Runs tab pulls from `/api/runs`; tapping a row opens the detail view,
which polls every 2 s while the run is pending/running. The Workflows tab
lists all registered workflows and lets you trigger one with an optional
JSON payload.

## Sideloading (no Play Store required)

For a personal client you don't need the Play Store:

1. Build the APK: `./gradlew assembleDebug`
2. Enable **Install unknown apps** for your file manager on the device
   (*Settings → Apps → Special app access*).
3. Transfer `app-debug.apk` to the device (ADB, email, cloud storage, etc.)
4. Open the APK from your file manager and tap **Install**.

For a slightly cleaner install (no debug overhead):

```bash
./gradlew assembleRelease
# then sign with your key:
apksigner sign --ks release.keystore \
    --out app-release-signed.apk \
    app/build/outputs/apk/release/app-release-unsigned.apk
```

You only need a signing key to sideload a release build; no Google Play
account required. Keep the keystore safe — you'll need the same key to
ship future updates that can be installed over the existing app.

## Background polling

The app uses `WorkManager` to optionally schedule periodic background
checks (battery-safe, deferred by Doze). The foreground detail view
polls every 2 s via a coroutine loop while the screen is on; WorkManager
is for the "did anything finish while the app was closed?" badge case.

## Cert trust

If you used `automaton tls init` and `--tls-cert`/`--tls-key` on the server:

- **Easy path**: install the cert as a trusted CA.
  Transfer `cert.pem` to the device, open it, and Android will prompt you
  to install it under *Settings → Security → Encryption & credentials →
  Install a certificate → CA certificate*.
- **Easier path (per-app)**: paste the cert's SHA-256 fingerprint into the
  app's Settings tab. `AutomatonClient` verifies the pin via OkHttp's
  `CertificatePinner` on every request; no system CA install needed.
  Trade-off: if you rotate the server cert, you must update the fingerprint
  in the app.

The network security config (`res/xml/network_security_config.xml`) allows
cleartext HTTP to RFC-1918 and `.local` / `.ts.net` addresses so you can
reach a local automaton instance without TLS while you're getting started.

## Push notifications

Not implemented in this initial cut. The Phase 7 ntfy.sh integration
already delivers phone notifications via the free ntfy Android app —
that's the recommended path for personal deployments.

If you want native FCM push later:

1. Add the `google-services.json` from Firebase Console to `app/`.
2. Add `com.google.firebase:firebase-messaging-ktx` dependency.
3. Implement `onMessageReceived` in a `FirebaseMessagingService`.
4. POST the registration token to a new server endpoint so automaton can
   call the FCM HTTP v1 API when a run completes or fails.

## CI

Linux CI can't build Android (requires Android SDK); the validation
on every PR is in `tests/test_deploy_android.py` and checks file
structure, Kotlin class names, and Gradle block presence against the
expected surface. The real build happens locally or on a Mac runner.
