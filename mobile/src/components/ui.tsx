import type { ReactNode } from "react";
import {
  ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View, type ScrollViewProps,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useAuth } from "../lib/auth";

export const color = {
  background: "#01060c",
  surface: "#071520",
  raised: "#10232f",
  line: "#1b3a48",
  ink: "#dff8ff",
  muted: "#9bbdc9",
  accent: "#00e5ff",
  good: "#5affc8",
  warn: "#ffcc78",
  error: "#ff9393",
};
export const type = StyleSheet.create({
  title: { color: color.ink, fontSize: 37, fontWeight: "700", letterSpacing: -1.2 },
  section: { color: color.ink, fontSize: 20, fontWeight: "700" },
  body: { color: color.ink, fontSize: 16, lineHeight: 24 },
  muted: { color: color.muted, fontSize: 14, lineHeight: 21 },
  eyebrow: { color: color.accent, fontSize: 11, fontWeight: "700", letterSpacing: 2.2 },
  error: { color: color.error, fontSize: 15, lineHeight: 22 },
  input: {
    color: color.ink, backgroundColor: color.surface, borderColor: color.line, borderWidth: 1,
    borderRadius: 12, fontSize: 17, minHeight: 54, padding: 14,
  },
});

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: color.background },
  scroll: { flexGrow: 1, paddingHorizontal: 20, paddingTop: 18, paddingBottom: 26, alignItems: "center" },
  inner: { width: "100%", maxWidth: 620, gap: 15 },
  header: { gap: 6, paddingBottom: 8 },
  card: { backgroundColor: color.surface, borderColor: color.line, borderWidth: 1, borderRadius: 16, padding: 16, gap: 7 },
  button: { minHeight: 48, minWidth: 48, alignItems: "center", justifyContent: "center", padding: 12, borderRadius: 12 },
  buttonText: { fontWeight: "700", fontSize: 16, textAlign: "center" },
  center: { flex: 1, justifyContent: "center", alignItems: "center", gap: 12 },
  badge: { color: color.warn, fontSize: 12, fontWeight: "700", letterSpacing: 0.7 },
});

export function Page({ title, children, ...scroll }: ScrollViewProps & { title: string; children: ReactNode }) {
  const { access } = useAuth();
  return (
    <SafeAreaView style={styles.safe} edges={["top", "left", "right"]}>
      <ScrollView {...scroll} contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
        <View style={styles.inner}>
          <View style={styles.header}>
            <Text style={type.eyebrow}>JARVIS  /  COMMAND CENTRE</Text>
            <Text accessibilityRole="header" style={type.title}>{title}</Text>
            {access.kind === "demo" && <Text style={styles.badge}>DEMO MODE · SAMPLE DATA</Text>}
          </View>
          {children}
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

export function Button({ label, onPress, primary = false, disabled = false }: {
  label: string; onPress: () => void; primary?: boolean; disabled?: boolean;
}) {
  return (
    <Pressable accessibilityRole="button" accessibilityState={{ disabled }} disabled={disabled} onPress={onPress}
      style={({ pressed }) => [styles.button, {
        opacity: disabled ? 0.45 : pressed ? 0.72 : 1,
        backgroundColor: primary ? color.accent : color.raised,
      }]}>
      <Text style={[styles.buttonText, { color: primary ? color.background : color.ink }]}>{label}</Text>
    </Pressable>
  );
}

export function Card({ children }: { children: ReactNode }) {
  return <View style={styles.card}>{children}</View>;
}

export function Loading({ text }: { text: string }) {
  return <View style={[styles.safe, styles.center]}><ActivityIndicator color={color.accent} /><Text style={type.muted}>{text}</Text></View>;
}

export function ErrorNote({ message, retry }: { message: string; retry?: () => void }) {
  return <Card><Text accessibilityRole="alert" style={type.error}>{message}</Text>{retry && <Button label="Try again" onPress={retry} />}</Card>;
}

export function label(state: string) {
  return state.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function londonTime(value: string | null | undefined) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not recorded";
  const onlyDate = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", dateStyle: "medium", ...(onlyDate ? {} : { timeStyle: "short" }),
  }).format(date);
}
