# Jarvis for iOS and Android

Jarvis is a native React Native app, not a browser wrapper. It reuses the hosted Command Centre API for the Today overview, Inbox and read-only text assistant. The desktop web app, Python services, and private Node/Claude voice bridge keep running independently. Native passkeys, voice and push notifications, and Instagram uploads or approvals are not part of this first release.

## Try it

Choose **Explore demo** on the sign-in screen to use sample data without network access. For live data, use the Command Centre passphrase. If a hosting security check blocks non-browser requests, the app stops rather than working around the check; live access requires the host to permit authenticated mobile API traffic. Verify access from the target phone before distributing a live release.

The Android preview is an APK. Download it to the device, open it and allow installs from that source if Android prompts. A local preview is not store-signed, so uninstall it before installing a build that uses a different signature. For iPhone, use a signed TestFlight build or an EAS internal build with the phone registered for ad hoc provisioning. An iOS simulator build cannot install on an iPhone.

## Develop and build

From this directory with Node 22.13+ or 24.3+:

```sh
npm ci
npm run lint
npm run typecheck
npm test
npx expo export --platform ios --platform android
npx expo start
```

For a standalone preview, link the project in the owner's Expo account with `npx eas-cli@latest init`, then run `npx eas-cli@latest build --platform android --profile preview` or `npx eas-cli@latest build --platform ios --profile preview`. Use `npx eas-cli@latest device:create` for iOS internal distribution. For a TestFlight release, use the production iOS build and EAS Submit with the owner's Apple Developer and App Store Connect access. Keep signing material out of this repo.

An Android-only local preview can also be built with the Android SDK and Java 17 or newer supported by the generated Gradle version:

```sh
npx expo prebuild --platform android --no-install
(cd android && ./gradlew :app:assembleRelease)
```

The APK is under `android/app/build/outputs/apk/release/`. Use the standard preview only for testing, not as a release signing key.

## How sign-in works

The app talks only to the fixed HTTPS URL for Command Centre, rejects redirects, and checks the API's unauthenticated health endpoint before sending a passphrase. It never stores the passphrase. It keeps a session in memory unless **Remember this device** is selected, then stores only the session credential in the OS secure store and revalidates it on launch. Existing server expiry, website session revocation, rate limiting, permissions and CSRF checks still apply. The mobile client has no vendor API keys and never talks directly to the desktop bridge.
