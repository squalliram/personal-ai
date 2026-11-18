import { useState } from "react";
import { Image, KeyboardAvoidingView, Platform, StyleSheet, Switch, Text, TextInput, View } from "react-native";
import { Button, Card, ErrorNote, Page, color, type } from "../components/ui";
import { problem } from "../lib/api";
import { useAuth } from "../lib/auth";

const mark = require("../../assets/icon.png");

export default function SignIn() {
  const { notice, signIn, startDemo } = useAuth();
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit() {
    if (busy || !password) return;
    setBusy(true);
    setError("");
    try {
      await signIn(password, remember);
    } catch (failure) {
      setError(problem(failure));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }
  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Page title="Welcome to Jarvis">
        <Image source={mark} style={styles.mark} accessibilityIgnoresInvertColors />
        <Text style={type.body}>Your personal Command Centre, on your phone.</Text>
        <Card>
          <Text style={type.section}>Sign in</Text>
          <Text style={type.muted}>Use your existing Command Centre passphrase.</Text>
          <TextInput accessibilityLabel="Command Centre passphrase" placeholder="Passphrase" placeholderTextColor={color.muted}
            style={type.input} autoCapitalize="none" autoCorrect={false} secureTextEntry autoComplete="current-password"
            editable={!busy} maxLength={1024} value={password} onChangeText={setPassword}
            returnKeyType="go" onSubmitEditing={() => void submit()} />
          <View style={styles.row}>
            <Text style={[type.body, styles.flex]}>Remember this device</Text>
            <Switch accessibilityLabel="Remember this device" value={remember} onValueChange={setRemember}
              disabled={busy} trackColor={{ false: color.line, true: color.accent }} thumbColor={color.ink} />
          </View>
          <Button primary label={busy ? "Signing in…" : "Sign in"} disabled={!password || busy} onPress={() => void submit()} />
        </Card>
        {(error || notice) && <ErrorNote message={error || notice || ""} />}
        <Button label="Explore demo" onPress={startDemo} disabled={busy} />
        <Text style={type.muted}>The demo uses sample data and doesn’t connect to your account.</Text>
      </Page>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  mark: { alignSelf: "center", width: 116, height: 116 },
  row: { flexDirection: "row", alignItems: "center", gap: 10, justifyContent: "space-between", minHeight: 48 },
  flex: { flexShrink: 1 },
});
