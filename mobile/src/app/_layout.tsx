import { DarkTheme, ThemeProvider } from "expo-router/react-navigation";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { AuthProvider, useAuth } from "../lib/auth";
import { color, Loading } from "../components/ui";

function Routes() {
  const { access, loading } = useAuth();
  if (loading) return <Loading text="Opening Jarvis…" />;
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Protected guard={access.kind === "signed-out"}>
        <Stack.Screen name="sign-in" />
      </Stack.Protected>
      <Stack.Protected guard={access.kind !== "signed-out"}>
        <Stack.Screen name="(command)" />
      </Stack.Protected>
    </Stack>
  );
}

const theme = {
  ...DarkTheme,
  colors: {
    ...DarkTheme.colors, background: color.background, card: color.surface,
    text: color.ink, primary: color.accent, border: color.line,
  },
};

export default function Layout() {
  return <AuthProvider><ThemeProvider value={theme}><StatusBar style="light" /><Routes /></ThemeProvider></AuthProvider>;
}
