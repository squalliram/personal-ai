import { useCallback, useState } from "react";
import { ActivityIndicator, Linking, RefreshControl, Text, View } from "react-native";
import { Button, Card, ErrorNote, Page, color, label, londonTime, type } from "../../components/ui";
import { api, useAuth } from "../../lib/auth";
import { demoInbox } from "../../lib/demo";
import { useResource } from "../../lib/use-resource";

export default function Inbox() {
  const { access } = useAuth();
  const [page, setPage] = useState(1);
  const [linkError, setLinkError] = useState(false);
  const load = useCallback((token: string) => api.inbox(token, page), [page]);
  const { data, error, loading, reload } = useResource(load, demoInbox);
  const hasPage = data?.page === page;

  async function openLink(link: string) {
    setLinkError(false);
    try {
      const url = new URL(link);
      if (url.protocol !== "https:" || !!url.username || !!url.password) return;
      await Linking.openURL(url.href);
    } catch {
      setLinkError(true);
    }
  }

  return (
    <Page title="Inbox" refreshControl={access.kind === "live"
      ? <RefreshControl refreshing={loading && hasPage} onRefresh={reload} tintColor={color.accent} colors={[color.accent]} />
      : undefined}>
      <Text style={type.muted}>{access.kind === "demo"
        ? "Sample updates from Instagram, energy and the daily briefing."
        : "Updates from your connected automations and assistants."}</Text>
      {(error || linkError) && <ErrorNote message={error ?? "Couldn’t open that link."} retry={error ? reload : undefined} />}
      {(loading || (!hasPage && !error)) && !hasPage &&
        <ActivityIndicator color={color.accent} accessibilityLabel="Loading inbox" />}
      {hasPage && data?.state !== "available" && <Card><Text style={type.body}>The Inbox is {label(data?.state ?? "unavailable").toLowerCase()}.</Text></Card>}
      {hasPage && data?.state === "available" && data.entries.length === 0 &&
        <Card><Text style={type.body}>No updates on this page.</Text></Card>}
      {hasPage && data?.entries.map((entry) => (
        <Card key={entry.id}>
          <Text style={[type.eyebrow, { color: entry.outcome === "failed" ? color.error : color.accent }]}>
            {label(entry.source)} · {entry.outcome === "ok" ? "Complete" : label(entry.outcome)}
          </Text>
          <Text style={type.section}>{entry.title}</Text>
          <Text style={type.muted}>{londonTime(entry.created_at)} · UK time</Text>
          {entry.link?.startsWith("https://") && <Button label="Open related link" onPress={() => void openLink(entry.link!)} />}
        </Card>
      ))}
      {(page > 1 || (hasPage && (data?.pages ?? 1) > 1)) && <View style={{ gap: 8 }}>
        <Text style={type.muted}>Page {page}{hasPage && data ? ` of ${data.pages}` : ""}</Text>
        {page > 1 && <Button label="Previous page" onPress={() => setPage(page - 1)} />}
        {hasPage && data && page < data.pages && <Button label="Next page" onPress={() => setPage(page + 1)} />}
      </View>}
    </Page>
  );
}
