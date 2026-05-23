# automaton iOS client

A small SwiftUI app that talks to a remote automaton server over its
HTTP API. Three tabs: Runs, Workflows, Settings.

| | |
|---|---|
| Min iOS | 17 |
| Architecture | SwiftUI + async/await; URLSession |
| Storage | UserDefaults for URL, Keychain for token |
| TLS | trusts system store OR pins a known self-signed cert by SHA-256 |
| Distribution | TestFlight (recommended for personal use) |

## Layout

```
deploy/ios/
  Package.swift
  Sources/
    AutomatonKit/                    Pure API client + Codable models
      Models.swift
      AutomatonClient.swift
    AutomatonApp/
      AutomatonApp.swift             @main + TabView
      Settings.swift                 Keychain-backed credential store
      Screens/
        RunsListView.swift           pull-to-refresh, color-coded statuses
        RunDetailView.swift          step tree + event log + cancel/signal
        WorkflowsView.swift          one-tap trigger with optional JSON payload
        SettingsView.swift           server URL + token + cert pin
```

`AutomatonKit` is split out as its own product so a future macOS
menubar or watchOS surface can pull it in without dragging the SwiftUI
views along.

## Build + run

On a Mac with Xcode 15+:

```bash
cd deploy/ios
open Package.swift     # Xcode opens it as a Swift Package
```

In Xcode:

1. Pick a simulator target (iPhone 15 Pro is fine) → press ⌘R.
2. In the running app, open the **Settings** tab.
3. Enter your server URL (`https://automaton.your-tailnet.ts.net:8443`
   if you followed the [mesh deploy guide](../mesh/README.md), or
   `https://192.168.x.y:8443` on a LAN).
4. Paste your `AUTOMATON_TOKEN` (the one in your server's env file).
5. If you used `automaton tls init` (self-signed cert) and don't want
   to install the cert as trusted on the device, paste the cert's
   SHA-256 fingerprint into the "self-signed cert pin" field.
   `automaton tls init` prints it after generation.
6. Tap **Test connection**. You should see "Connection OK".

The Runs tab pulls from `/api/runs`; tapping a row opens the detail
view which polls every 2 s while the run is pending/running. The
Workflows tab lists workflow names seen in the run history (the server
doesn't expose a definitions GET today) and gives you a one-tap
trigger with an optional JSON payload editor.

## TestFlight

Personal-use distribution path:

1. Apple Developer Program ($99/year). Personal account is fine; no
   business entity required.
2. In Xcode: **Signing & Capabilities** → pick your Team, set a
   bundle ID like `io.your-name.automaton`.
3. **Product → Archive**, then in the Organizer window choose
   **Distribute App → TestFlight Internal Only**.
4. Wait ~5 minutes for processing; install via the TestFlight app on
   your phone using the same Apple ID you used to upload.

TestFlight builds expire after 90 days but you can ship a new build
any time. For internal testers (up to 100 devices), no Apple review.

**Don't ship to the App Store** for a personal client like this. App
Review will hassle you about a server with a custom protocol and
self-signed cert paths, and the cost / benefit is upside-down. The
plan ([Phase 14](../../PLATFORM-EXPANSION-PLAN.md)) explicitly
recommends TestFlight as the stopping point for personal infra.

## Push notifications

Not implemented in this initial cut. If you want them:

1. Add an `aps-environment` entitlement to the app.
2. Implement `application(_:didRegisterForRemoteNotificationsWithDeviceToken:)`
   to ship the APNs token back to your server via a new POST endpoint.
3. Server-side, ship a notify hook that signs an APNs payload with
   your Apple key and posts to `https://api.push.apple.com`.

That's roughly a week of work on top of the existing app. Most personal
users won't need it - the Phase 7 ntfy.sh notifications already buzz
your phone via the ntfy iOS app for free, with no Apple Developer
Program required. Phase 14 in the plan notes this trade-off explicitly.

## Cert trust dance

If you used `automaton tls init` and `--tls-cert`/`--tls-key` on the
server (see Phase 4):

- **Easy path**: install the cert as trusted system-wide. AirDrop
  `cert.pem` to your iPhone, open it, install the profile, then go to
  *Settings → General → About → Certificate Trust Settings* and flip
  the toggle on for the cert.
- **Easier path (per-app)**: paste the cert's SHA-256 fingerprint into
  the app's Settings tab. The app's `URLSession` delegate verifies the
  pin on every request; no system cert install needed. Trade-off: if
  you rotate the server cert, you must update the fingerprint in the
  app.

The fingerprint pin is the right answer for personal-infra
deployments. The cert-trust approach is the right answer if multiple
apps on the device need to talk to the same server.

## CI

The Linux CI runners can't build Swift; the validation we run on
every PR is in `tests/test_deploy_ios.py` and verifies file structure
and method names against the Python client's surface. The real build
should happen on a Mac via `xcodebuild` or Xcode's native flow.
