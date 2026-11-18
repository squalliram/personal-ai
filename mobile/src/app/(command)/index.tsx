import { ActivityIndicator, RefreshControl, Text } from "react-native";
import { Card, ErrorNote, Page, color, label, londonTime, type } from "../../components/ui";
import { api, useAuth } from "../../lib/auth";
import { demoOverview } from "../../lib/demo";
import { useResource } from "../../lib/use-resource";

export default function Today() {
  const { access } = useAuth();
  const { data, error, loading, reload } = useResource(api.overview, demoOverview);
  return (
    <Page title="Today" refreshControl={access.kind === "live"
      ? <RefreshControl refreshing={loading && !!data} onRefresh={reload} tintColor={color.accent} colors={[color.accent]} />
      : undefined}>
      {error && <ErrorNote message={error} retry={reload} />}
      {loading && !data && <ActivityIndicator color={color.accent} accessibilityLabel="Loading overview" />}
      {data && <>
        <Card>
          <Text style={type.eyebrow}>SYSTEM STATUS</Text>
          <Text style={type.section}>{label(data.status.state)}</Text>
          <Text style={type.body}>{data.status.automations_healthy} of {data.status.automations_monitored} monitored automations healthy</Text>
          <Text style={type.muted}>{access.kind === "demo" ? "Sample snapshot" : "Updated"} · {londonTime(data.generated_at)} · UK time</Text>
        </Card>
        <Text accessibilityRole="header" style={type.section}>Needs attention</Text>
        {data.attention.length === 0 && <Text style={type.muted}>Nothing needs your attention.</Text>}
        {data.attention.map((item, index) => (
          <Card key={`${item.source}-${index}`}>
            <Text style={[type.eyebrow, { color: item.severity === "critical" ? color.error : color.warn }]}>
              {label(item.severity)}
            </Text>
            <Text style={type.body}>{item.fact}</Text>
          </Card>
        ))}
        <Card>
          <Text style={type.eyebrow}>INSTAGRAM</Text>
          <Text style={type.section}>{data.instagram.pending_drafts == null
            ? label(data.instagram.status) : `${data.instagram.pending_drafts} pending drafts`}</Text>
          <Text style={type.muted}>{data.instagram.summary}</Text>
          {data.instagram.next_slot && <Text style={type.body}>Next slot · {londonTime(data.instagram.next_slot)}</Text>}
        </Card>
        <Card>
          <Text style={type.eyebrow}>ENERGY</Text>
          <Text style={type.section}>{label(data.energy.state)}</Text>
          <Text style={type.muted}>Last sync · {londonTime(data.energy.last_successful_sync_at)}</Text>
        </Card>
        <Card>
          <Text style={type.eyebrow}>DAILY BRIEFING</Text>
          <Text style={type.body}>{londonTime(data.briefing.latest_report_date)}</Text>
        </Card>
      </>}
    </Page>
  );
}
