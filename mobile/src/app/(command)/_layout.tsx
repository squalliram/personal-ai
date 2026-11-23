import { Tabs } from "expo-router";
import { Platform, Text, type ColorValue } from "react-native";
import { color } from "../../components/ui";

function tabIcon(mark: string) {
  return function TabIcon({ color: ink }: { color: ColorValue | null }) {
    return <Text accessible={false} importantForAccessibility="no" style={{ color: ink ?? color.muted, fontSize: 20 }}>{mark}</Text>;
  };
}

export default function CommandTabs() {
  return (
    <Tabs screenOptions={{
      headerShown: false,
      tabBarActiveTintColor: color.accent,
      tabBarInactiveTintColor: color.muted,
      tabBarStyle: { backgroundColor: color.surface, borderTopColor: color.line },
      tabBarLabelStyle: { fontSize: 14, fontWeight: "600" },
    }}>
      <Tabs.Screen name="index" options={{ title: "Today", tabBarIcon: tabIcon("◉") }} />
      <Tabs.Screen name="ask" options={{ title: "Ask", tabBarIcon: tabIcon("✦") }} />
      <Tabs.Screen name="bot" options={{ title: "Devin Bot", href: Platform.OS === "ios" ? "/bot" : null, tabBarIcon: tabIcon("▤") }} />
      <Tabs.Screen name="inbox" options={{ title: "Inbox", href: null }} />
      <Tabs.Screen name="settings" options={{ title: "Settings", tabBarIcon: tabIcon("⚙") }} />
    </Tabs>
  );
}
