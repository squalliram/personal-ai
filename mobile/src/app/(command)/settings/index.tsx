import { router } from "expo-router";
import { useState } from "react";
import { Text } from "react-native";
import { Button, Card, Page, type } from "../../../components/ui";
import { useAuth } from "../../../lib/auth";

export default function Settings() {
  const { access, signOut } = useAuth();
  const [busy, setBusy] = useState(false);
  const demo = access.kind === "demo";
  async function exit() {
    if (busy) return;
    setBusy(true);
    try { await signOut(); } finally { setBusy(false); }
  }
  return (
    <Page title="Settings">
      <Card>
        <Text style={type.eyebrow}>ACCOUNT</Text>
        <Text style={type.section}>{demo ? "Demo session" : "Command Centre"}</Text>
        <Text style={type.muted}>{demo
          ? "This session uses sample data. No account is connected."
          : access.kind === "live" && access.session.remembered
            ? "This device is remembered. You can revoke its session from website Settings."
            : "Your session isn’t saved on this device."}</Text>
      </Card>
      <Button label="Open update history" onPress={() => router.push("/settings/history")} />
      <Button label={busy ? "Signing out…" : demo ? "Exit demo" : "Sign out"} disabled={busy} onPress={() => void exit()} />
    </Page>
  );
}
