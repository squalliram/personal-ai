import type { Attention } from "./api";

const WEBSITE = "https://iramkhan.com/command-centre/";

export function priorityItems(items: Attention[]) {
  return [
    ...items.filter(({ severity }) => severity === "action"),
    ...items.filter(({ severity }) => severity === "critical" || severity === "warning"),
  ];
}

export function attentionDestination(item: Attention) {
  switch (item.href) {
    case "#instagram":
      return {
        label: item.severity === "action" ? "Review drafts on website" : "Open Instagram details on website",
        url: WEBSITE + "#instagram",
      };
    case "#automations": return { label: "Open automation details on website", url: WEBSITE + "#automations" };
    case "#energy": return { label: "Open energy details on website", url: WEBSITE + "#energy" };
  }
}
