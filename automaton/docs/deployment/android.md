# Android client

The Android app is a Jetpack Compose app (Kotlin) that connects to your automaton server over HTTPS.

Source: `deploy/android/`

## Features

- Runs list with status badges (pending, running, completed, failed, cancelled, timed_out)
- Run detail with step tree and live polling while a run is active
- Workflows list with one-tap trigger
- Signal responder — send a response to a parked `wait_for_signal` step
- SHA-256 certificate pinning for self-signed certs
- Token stored in `EncryptedSharedPreferences` (AES-256-GCM)
- Background polling via WorkManager

## Install (sideload)

1. Build the APK with Android Studio or Gradle:
   ```bash
   cd deploy/android
   ./gradlew assembleRelease
   ```
2. Copy `app/build/outputs/apk/release/app-release.apk` to your phone.
3. Enable "Install unknown apps" for your file manager in Android Settings.
4. Tap the APK to install.

No Play Store account required for personal use.

## First-time setup

1. Open the app → **Settings** tab.
2. Enter your server URL (e.g. `https://automaton.your-tailnet.ts.net:8080`).
3. Paste your bearer token (`AUTOMATON_TOKEN` from your env file).
4. Optionally paste the SHA-256 fingerprint of your TLS cert for pinning.

## Certificate pinning

For a self-signed cert, get the fingerprint:

```bash
openssl x509 -in tls/cert.pem -noout -fingerprint -sha256 \
    | sed 's/SHA256 Fingerprint=//' | tr -d ':'
```

Paste the result into the app's **Cert fingerprint** field. The app will reject any connection whose certificate doesn't match.

## Requirements

- Android 8.0 (API 26) or later
- The automaton server reachable over HTTPS (use [Mesh networking](mesh.md) or TLS + port forwarding)
