import { router, useFocusEffect } from "expo-router";
import { useCallback, useRef, useState } from "react";
import { ActivityIndicator, Linking, Platform, RefreshControl, Text } from "react-native";
import { Button, Card, ErrorNote, Page, color, label, londonTime, type } from "../../components/ui";
import { attentionDestination, priorityItems } from "../../lib/attention";
import { api, useAuth } from "../../lib/auth";
import { demoOverview } from "../../lib/demo";
import { useResource } from "../../lib/use-resource";

export default function Today() {
  const { access } = useAuth();
  const { data, error, loading, reload } = useResource(api.overview, demoOverview);
  const [linkError, setLinkError] = useState(false);
  const firstFocus = useRef(true);
  useFocusEffect(useCallback(() => {
    if (firstFocus.current) firstFocus.current = false;
    else if (access.kind === "live") reload();
  }, [access.kind, reload]));
  const items = data ? priorityItems(data.attention) : [];

  async function openDetails(url: string) {
    setLinkError(false);
    try { await Linking.openURL(url); } catch { setLinkError(true); }
  }

  return (
    <Page title="Today" refreshControl={access.kind === "live"
      ? <RefreshControl refreshing={loading && !!data} onRefresh={reload} tintColor={color.accent} colors={[color.accent]} />
      : undefined}>
      <Text accessibilityRole="header" style={type.section}>Needs attention</Text>
      {error && <ErrorNote message={error} retry={reload} />}
      {linkError && <ErrorNote message="Couldn’t open Command Centre in your browser." />}
      {loading && !data && <ActivityIndicator color={color.accent} accessibilityLabel="Loading attention items" />}
      {data && <>
        {items.length === 0 && <Card><Text style={type.body}>Nothing needs your attention right now.</Text></Card>}
        {items.map((item) => {
          const destination = access.kind === "live" ? attentionDestination(item) : undefined;
          return (
            <Card key={`${item.source}-${item.fact}`}>
              <Text style={[type.eyebrow, { color: item.severity === "critical" ? color.error : item.severity === "warning" ? color.warn : color.accent }]}>
                {item.severity === "action" ? "Action needed" : label(item.severity)}
              </Text>
              <Text style={type.body}>{item.fact}</Text>
              {destination && <Button label={destination.label} onPress={() => void openDetails(destination.url)} />}
            </Card>
          );
        })}
        {access.kind === "live" && <Text style={type.muted}>Updated {londonTime(data.generated_at)} · UK time</Text>}
      </>}
      <Text accessibilityRole="header" style={type.section}>Quick actions</Text>
      <Button primary label="Ask Jarvis" onPress={() => router.navigate("/ask")} />
      {Platform.OS === "ios" && <Button label="Message Devin Bot" onPress={() => router.navigate("/bot")} />}
    </Page>
  );
}
