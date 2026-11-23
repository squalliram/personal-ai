# Jarvis for iOS and Android

Jarvis is a native React Native app, not a browser wrapper. Today shows the same action items and issues as the website's My Day view, without the full dashboard. Follow an alert to its source on the website for details or actions such as draft approval. The read-only Ask Jarvis assistant is in its own tab, and update history is under Settings. The desktop web app, Python services and private Node/Claude bridge run independently.

## Try it

Choose **Explore demo** on the sign-in screen to use sample data without network access. For live data, use the Command Centre passphrase. If a hosting security check blocks non-browser requests, the app stops rather than working around the check; live access requires the host to permit authenticated mobile API traffic. Verify access from the target phone before distributing a live release.

### Dictation and Devin Bot on iPhone

- In **Ask**, tap **Dictate a question**, then the microphone on the iPhone keyboard. Review the text before submitting. If the microphone is missing, check **Settings → General → Keyboard → Enable Dictation**. Jarvis doesn't capture audio; dictation uses the system keyboard.
- [Link your Devin Bot in Devin](https://docs.devin.ai/work-with-devin/bots/imessage#link-your-number) first. In Jarvis's **Devin Bot** tab, save the bot's number from the linked Messages conversation with its country code. The number stays on the device. Dictate or type a draft, then tap **Review in Messages**, check the recipient and send from Apple's composer. Jarvis doesn't send automatically, link your account itself, or read your replies. Use **Open Messages** to return to the conversation.
- Demo mode lets you try dictation without sending anything. Android can use keyboard dictation in Ask, but doesn't have the iMessage handoff.

The Android preview is an APK. Download it from the repository's GitHub Actions run, open it on the device and allow installs from that source if Android prompts. Each CI preview has its own signing key; uninstall an older preview before installing a new one. For iPhone, use a signed TestFlight build or an EAS internal build with the phone registered for ad hoc provisioning. An iOS simulator build cannot install on an iPhone.

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

The APK is under `android/app/build/outputs/apk/release/`. The default local build uses the public template debug key; use that build for demo testing only, not for live sign-in or distribution.

## How sign-in works

The app talks only to the fixed HTTPS URL for Command Centre, rejects redirects, and checks the API's unauthenticated health endpoint before sending a passphrase. It never stores the passphrase. It keeps a session in memory unless **Remember this device** is selected, then stores only the session credential in the OS secure store and revalidates it on launch. Existing server expiry, website session revocation, rate limiting, permissions and CSRF checks still apply. The mobile client has no vendor API keys and never talks directly to the desktop bridge.
