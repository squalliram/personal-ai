#!/usr/bin/env python3
"""Send a failure alert through the notify-owner path and optional Bot webhook.

Run by .github/workflows/alert-on-failure.yml on failed workflow_run events or a forced
failure test dispatch. Reads event metadata from GITHUB_EVENT_PATH so untrusted strings
never enter step env listings or shell commands in this public repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notifications import NotifyError, safe_link, send_webhook  # noqa: E402

FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
RUN_ID = re.compile(r"[0-9]+")


def _load_notify_owner():
    spec = importlib.util.spec_from_file_location("notify_owner", ROOT / "scripts" / "notify-owner.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load scripts/notify-owner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_text(value: object, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", value).split())
    return cleaned[:limit].rstrip()


def _run_url(env: Mapping[str, str]) -> str:
    repo = env.get("GITHUB_REPOSITORY", "").strip()
    run_id = env.get("GITHUB_RUN_ID", "").strip()
    if REPO_NAME.fullmatch(repo) and RUN_ID.fullmatch(run_id):
        return f"https://github.com/{repo}/actions/runs/{run_id}"
    return ""


def build_alert(event: Mapping[str, object] | None, env: Mapping[str, str]) -> dict[str, object] | None:
    """Return a failure alert payload dict, or None when the event should not alert."""
    if isinstance(event, Mapping) and isinstance(event.get("workflow_run"), Mapping):
        run = event["workflow_run"]
        conclusion = _clean_text(run.get("conclusion"), 32)
        if conclusion not in FAILURE_CONCLUSIONS:
            return None
        repo_obj = event.get("repository")
        repo = _clean_text(repo_obj.get("full_name"), 120) if isinstance(repo_obj, Mapping) else ""
        head_repo_obj = run.get("head_repository")
        head_repo = _clean_text(head_repo_obj.get("full_name"), 120) if isinstance(head_repo_obj, Mapping) else ""
        if repo and head_repo and head_repo != repo:
            return None
        trigger = _clean_text(run.get("event"), 40)
        if trigger == "pull_request":
            return None
        name = _clean_text(run.get("name"), 80) or "Workflow"
        branch = _clean_text(run.get("head_branch"), 60)
        if name == "Test" and (branch != "main" or trigger != "push"):
            return None
        sha = _clean_text(run.get("head_sha"), 40)
        short_sha = sha[:7] if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha) else ""
        context = conclusion
        if branch:
            context += f" on {branch}"
        if short_sha:
            context += f", {short_sha}"
        title = f"Jarvis run failed: {name} ({context})"
        link = safe_link(run.get("html_url")) or _run_url(env)
        alert: dict[str, object] = {
            "source": "jarvis",
            "outcome": "failed",
            "title": title,
            "workflow": name,
            "conclusion": conclusion,
        }
        if link:
            alert["link"] = link
        if trigger:
            alert["event"] = trigger
        if branch:
            alert["branch"] = branch
        if sha:
            alert["sha"] = sha
        if isinstance(run.get("id"), int):
            alert["run_id"] = run["id"]
        if repo:
            alert["repository"] = repo
        return alert

    if isinstance(event, Mapping) and (
        event.get("action") == "jarvis-alert-test" or "inputs" in event or "client_payload" in event
    ):
        inputs = event.get("inputs") or event.get("client_payload") or {}
        source = _clean_text(inputs.get("source"), 80) if isinstance(inputs, Mapping) else ""
        source = source or "Forced failure test"
        link = _run_url(env)
        alert = {
            "source": "jarvis",
            "outcome": "failed",
            "title": f"Jarvis run failed: {source} (forced failure test)",
            "workflow": source,
            "conclusion": "failure",
            "forced_test": True,
        }
        if link:
            alert["link"] = link
        repo = env.get("GITHUB_REPOSITORY", "").strip()
        if REPO_NAME.fullmatch(repo):
            alert["repository"] = repo
        return alert

    title = _clean_text(env.get("ALERT_TITLE", ""), 200)
    if not title:
        raise ValueError("No workflow_run event or ALERT_TITLE provided")
    alert = {
        "source": "jarvis",
        "outcome": "failed",
        "title": title,
        "workflow": _clean_text(env.get("ALERT_WORKFLOW", ""), 80) or "Manual alert",
        "conclusion": _clean_text(env.get("ALERT_CONCLUSION", ""), 32) or "failure",
    }
    link = safe_link(env.get("ALERT_LINK", "").strip()) or _run_url(env)
    if link:
        alert["link"] = link
    return alert


def load_event(env: Mapping[str, str]) -> Mapping[str, object] | None:
    path = env.get("GITHUB_EVENT_PATH", "").strip()
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("GITHUB_EVENT_PATH must contain a JSON object")
    return data


def dispatch(
    alert: Mapping[str, object],
    env: Mapping[str, str],
    *,
    notify_owner=None,
    webhook_sender: Callable[..., None] = send_webhook,
) -> int:
    """Deliver alert through notify-owner and optional JARVIS_ALERT_WEBHOOK_URL."""
    notify_mod = notify_owner or _load_notify_owner()
    notify_env = {
        key: env[key]
        for key in (
            "NOTIFY_INGEST_TOKEN",
            "COMMAND_CENTRE_URL",
            "CPANEL_SSH_HOST",
            "CPANEL_SSH_USERNAME",
            "CPANEL_SSH_PORT",
        )
        if key in env
    }
    notify_env["NOTIFY_TITLE"] = str(alert["title"])
    notify_env["NOTIFY_OUTCOME"] = "failed"
    if alert.get("link"):
        notify_env["NOTIFY_LINK"] = str(alert["link"])

    notify_ok = True
    try:
        if target := notify_mod.ssh_target(notify_env):
            code = notify_mod.relay(notify_env, target)
            if code != 0:
                notify_ok = False
        else:
            stored = notify_mod.send(notify_env)
            print(f"Notification stored (inbox ID {stored}).")
    except Exception as exc:  # noqa: BLE001
        notify_ok = False
        print(f"Alert notify-owner delivery failed: {exc}", file=sys.stderr)

    webhook_ok = True
    webhook_url = safe_link(env.get("JARVIS_ALERT_WEBHOOK_URL", "").strip())
    if webhook_url:
        secret = env.get("JARVIS_ALERT_WEBHOOK_SECRET", "").strip()
        try:
            webhook_sender(webhook_url, alert, secret=secret)
            print("Alert webhook delivered.")
        except (NotifyError, OSError, ValueError) as exc:
            webhook_ok = False
            print(f"Alert webhook delivery failed: {exc}", file=sys.stderr)

    return 0 if (notify_ok and webhook_ok) else 1


def main() -> int:
    env = dict(os.environ)
    try:
        event = load_event(env)
        alert = build_alert(event, env)
    except (OSError, ValueError) as exc:
        print(f"Alert skipped: {exc}", file=sys.stderr)
        return 1
    if alert is None:
        print("Event does not require a failure alert.")
        return 0
    return dispatch(alert, env)


if __name__ == "__main__":
    sys.exit(main())
