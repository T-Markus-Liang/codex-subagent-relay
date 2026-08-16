#!/usr/bin/env python3
"""Build a source-separated portable operations-dashboard artifact from reviewed JSON reports."""

from __future__ import annotations

import argparse
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def number(value: Any) -> float | int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def relative_source(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path.name


def relay_summary(report: dict[str, Any]) -> dict[str, Any]:
    overall = report.get("overall") if isinstance(report.get("overall"), dict) else {}
    usage = overall.get("usage") if isinstance(overall.get("usage"), dict) else {}
    accepted = usage.get("accepted_success_usage") if isinstance(usage.get("accepted_success_usage"), dict) else {}
    window = report.get("utc_window") if isinstance(report.get("utc_window"), dict) else {}
    return {
        "window_start": str(window.get("start") or "unknown"),
        "window_end": str(window.get("end") or "unknown"),
        "runs": number(overall.get("runs")),
        "successes": number(overall.get("successes")),
        "success_rate_percent": number(overall.get("success_rate_percent")),
        "p95_duration_seconds": number(overall.get("p95_duration_seconds")),
        "partial_write_runs": number(overall.get("partial_write_runs")),
        "finalization_recovery_runs": number(overall.get("finalization_recovery_runs")),
        "fallback_runs": number(overall.get("fallback_runs")),
        "automatic_runs": number((overall.get("requested_provider_counts") or {}).get("auto")),
        "explicit_runs": sum(number(value) for key, value in (overall.get("requested_provider_counts") or {}).items() if key not in {"auto", "unknown"}),
        "accepted_request_tokens": number(accepted.get("request_tokens")),
        "accepted_context_tokens": number(accepted.get("context_tokens")),
    }


def provider_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    providers = report.get("providers") if isinstance(report.get("providers"), dict) else {}
    rows: list[dict[str, Any]] = []
    for provider, detail in sorted(providers.items()):
        if not isinstance(detail, dict):
            continue
        usage = detail.get("usage") if isinstance(detail.get("usage"), dict) else {}
        accepted = usage.get("accepted_success_usage") if isinstance(usage.get("accepted_success_usage"), dict) else {}
        rows.append({
            "provider": provider,
            "runs": number(detail.get("runs")),
            "success_rate_percent": number(detail.get("success_rate_percent")),
            "p95_duration_seconds": number(detail.get("p95_duration_seconds")),
            "fallback_runs": number(detail.get("fallback_runs")),
            "partial_write_runs": number(detail.get("partial_write_runs")),
            "finalization_recovery_runs": number(detail.get("finalization_recovery_runs")),
            "accepted_request_tokens": number(accepted.get("request_tokens")),
        })
    return rows


def usage_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    sections = report.get("sources") if isinstance(report.get("sources"), list) else []
    rows: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        source = str(section.get("source") or "unknown")
        for row in section.get("rows") if isinstance(section.get("rows"), list) else []:
            if not isinstance(row, dict):
                continue
            model = str(row.get("model") or "unknown")
            if source == "codex_rollout":
                metric = number(row.get("total_tokens"))
                label = "recorded_total_tokens"
            else:
                metric = number(row.get("context_tokens"))
                label = "context_tokens_including_cache"
            rows.append({
                "ledger": source,
                "model": model,
                "metric": label,
                "tokens": metric,
                "share_percent": number(row.get("share_percent")),
            })
    return rows


def build_artifact(relay_report: dict[str, Any], relay_path: Path, usage_report: dict[str, Any] | None, usage_path: Path | None) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    summary = relay_summary(relay_report)
    sources = [{"id": "relay_operations", "label": "Relay operational telemetry", "path": relative_source(relay_path)}]
    datasets: dict[str, list[dict[str, Any]]] = {
        "relay_summary": [summary],
        "provider_health": provider_rows(relay_report),
    }
    access_issues: list[dict[str, str]] = []
    if usage_report is not None and usage_path is not None:
        sources.append({"id": "codex_usage_audit", "label": "Codex Rollout and CC Switch usage audit", "path": relative_source(usage_path)})
        datasets["source_usage"] = usage_rows(usage_report)
    else:
        access_issues.append({"id": "usage_audit_not_supplied", "dataset": "source_usage", "message": "Run codex-usage-audit separately and provide its reviewed JSON output to show Codex Rollout and CC Switch ledgers."})

    cards = [
        {"id": "relay_success", "description": "Complete success rate for version-filtered Relay production runs.", "dataset": "relay_summary", "sourceId": "relay_operations", "metrics": [{"label": "Relay success rate", "field": "success_rate_percent", "format": "number", "unit": "%"}]},
        {"id": "relay_latency", "description": "P95 end-to-end Relay duration in seconds.", "dataset": "relay_summary", "sourceId": "relay_operations", "metrics": [{"label": "P95 latency", "field": "p95_duration_seconds", "format": "number", "unit": "s"}]},
        {"id": "relay_partial", "description": "Partial writes require manual review and never auto-retry.", "dataset": "relay_summary", "sourceId": "relay_operations", "metrics": [{"label": "Partial writes", "field": "partial_write_runs", "format": "number"}]},
        {"id": "relay_finalization", "description": "No-tool same-session attempts used to recover a missing final contract.", "dataset": "relay_summary", "sourceId": "relay_operations", "metrics": [{"label": "Finalization recovery", "field": "finalization_recovery_runs", "format": "number"}]},
        {"id": "relay_automatic", "description": "Jobs requested through the production automatic route, distinct from explicit diagnostics.", "dataset": "relay_summary", "sourceId": "relay_operations", "metrics": [{"label": "Automatic jobs", "field": "automatic_runs", "format": "number"}]},
        {"id": "relay_effective_tokens", "description": "Relay-local successful final-attempt request tokens only; excludes errors, partials, and retries.", "dataset": "relay_summary", "sourceId": "relay_operations", "metrics": [{"label": "Effective Relay tokens", "field": "accepted_request_tokens", "format": "number"}]},
    ]
    blocks: list[dict[str, Any]] = [
        {"id": "overview", "type": "markdown", "body": "# Relay Operations Dashboard\n\nRelay, Codex Rollout, and CC Switch are separate ledgers. This dashboard never adds their token totals."},
        {"id": "relay_metrics", "type": "metric-strip", "cardIds": [card["id"] for card in cards]},
        {"id": "provider_table", "type": "table", "tableId": "provider_health", "layout": "full"},
    ]
    tables = [{
        "id": "provider_health", "title": "Relay provider health", "subtitle": "Version-filtered external production runs only.", "dataset": "provider_health", "sourceId": "relay_operations", "defaultSort": {"field": "success_rate_percent", "direction": "desc"}, "layout": "full", "columns": [
            {"field": "provider", "label": "Provider", "type": "text"},
            {"field": "runs", "label": "Runs", "format": "number"},
            {"field": "success_rate_percent", "label": "Success rate", "format": "number", "unit": "%"},
            {"field": "p95_duration_seconds", "label": "P95 seconds", "format": "number"},
            {"field": "fallback_runs", "label": "Fallback", "format": "number"},
            {"field": "partial_write_runs", "label": "Partial writes", "format": "number"},
            {"field": "finalization_recovery_runs", "label": "Finalization recovery", "format": "number"},
            {"field": "accepted_request_tokens", "label": "Effective Relay tokens", "format": "number"},
        ],
    }]
    if usage_report is not None:
        blocks.append({"id": "usage_table", "type": "table", "tableId": "source_usage", "layout": "full"})
        tables.append({
            "id": "source_usage", "title": "Codex and proxy ledgers", "subtitle": "Shares are only within each ledger. Do not add rows across ledgers or to Relay telemetry.", "dataset": "source_usage", "sourceId": "codex_usage_audit", "defaultSort": {"field": "tokens", "direction": "desc"}, "layout": "full", "columns": [
                {"field": "ledger", "label": "Ledger", "type": "text"},
                {"field": "model", "label": "Model", "type": "text"},
                {"field": "metric", "label": "Metric", "type": "text"},
                {"field": "tokens", "label": "Tokens", "format": "number"},
                {"field": "share_percent", "label": "Within-ledger share", "format": "number", "unit": "%"},
            ],
        })
    return {
        "surface": "dashboard",
        "manifest": {"version": 1, "surface": "dashboard", "title": "Relay Operations Dashboard", "description": "Source-separated reliability and usage monitoring for Codex Subagent Relay.", "generatedAt": generated_at, "cards": cards, "charts": [], "tables": tables, "sources": sources, "blocks": blocks},
        "snapshot": {"version": 1, "generatedAt": generated_at, "status": "ready" if not access_issues else "partial", "datasets": datasets, **({"accessIssues": access_issues} if access_issues else {})},
        "sources": sources,
        "package_info": {"generator": "scripts/operations_dashboard.py", "ledger_boundary": "Relay telemetry, Codex Rollout, and CC Switch totals are separate and must not be added."},
    }


def display(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def table_html(columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> str:
    headers = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    if not rows:
        body = f"<tr><td colspan=\"{len(columns)}\">No matching rows in this observation window.</td></tr>"
    else:
        body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(display(row.get(field, 0)))}</td>" for field, _ in columns) + "</tr>"
            for row in rows
        )
    return f"<div class=\"table-wrap\"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div>"


def render_html(artifact: dict[str, Any]) -> str:
    snapshot = artifact["snapshot"]
    datasets = snapshot["datasets"]
    summary = datasets["relay_summary"][0]
    sources = artifact["sources"]
    cards = [
        ("Relay success rate", f"{display(summary['success_rate_percent'])}%", "Complete production runs"),
        ("P95 latency", f"{display(summary['p95_duration_seconds'])} s", "End-to-end Relay duration"),
        ("Partial writes", display(summary["partial_write_runs"]), "Require manual diff review"),
        ("Finalization recovery", display(summary["finalization_recovery_runs"]), "Same session, no tools"),
        ("Automatic jobs", display(summary["automatic_runs"]), "Requested through auto routing"),
        ("Effective Relay tokens", display(summary["accepted_request_tokens"]), "Final success attempts only"),
    ]
    card_html = "".join(
        f"<section class=\"metric\"><p>{html.escape(label)}</p><strong>{html.escape(value)}</strong><span>{html.escape(note)}</span></section>"
        for label, value, note in cards
    )
    provider = table_html(
        [("provider", "Provider"), ("runs", "Runs"), ("success_rate_percent", "Success rate %"), ("p95_duration_seconds", "P95 seconds"), ("fallback_runs", "Fallback"), ("partial_write_runs", "Partial writes"), ("finalization_recovery_runs", "Finalization recovery"), ("accepted_request_tokens", "Effective Relay tokens")],
        datasets["provider_health"],
    )
    usage_section = ""
    if "source_usage" in datasets:
        usage_section = "<section><h2>Codex And Proxy Ledgers</h2><p>Each share is valid only within its own ledger. These rows are not added to Relay usage.</p>" + table_html(
            [("ledger", "Ledger"), ("model", "Model"), ("metric", "Metric"), ("tokens", "Tokens"), ("share_percent", "Within-ledger share %")], datasets["source_usage"]
        ) + "</section>"
    else:
        issue = snapshot.get("accessIssues", [{}])[0].get("message", "Usage audit was not supplied.")
        usage_section = f"<section class=\"notice\"><h2>Codex And Proxy Ledgers</h2><p>{html.escape(issue)}</p></section>"
    source_html = "".join(
        f"<li><strong>{html.escape(str(source['label']))}</strong><br><code>{html.escape(str(source['path']))}</code></li>" for source in sources
    )
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Relay Operations Dashboard</title>
<style>
:root {{ color-scheme: light; --ink:#162033; --muted:#5d687a; --line:#d8dee8; --bg:#f5f7fa; --panel:#fff; --good:#0f766e; --warn:#b45309; --accent:#2563eb; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:1240px; margin:0 auto; padding:32px 24px 56px; }} header {{ border-bottom:1px solid var(--line); padding-bottom:22px; }} h1 {{ margin:0; font-size:28px; }} h2 {{ margin:30px 0 8px; font-size:18px; }} p {{ color:var(--muted); margin:6px 0 0; }} .warning {{ margin:20px 0; padding:12px 14px; border-left:4px solid var(--warn); background:#fff7ed; color:#7c2d12; }} .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:22px; }} .metric {{ min-height:132px; padding:16px; background:var(--panel); border:1px solid var(--line); border-radius:6px; }} .metric p {{ margin:0; font-weight:600; color:var(--ink); }} .metric strong {{ display:block; margin:15px 0 6px; font-size:26px; color:var(--good); }} .metric span {{ color:var(--muted); font-size:12px; }} section {{ margin-top:26px; }} .table-wrap {{ overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:6px; }} table {{ width:100%; border-collapse:collapse; min-width:720px; }} th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }} th {{ background:#eef2f7; color:#344054; font-size:12px; }} tr:last-child td {{ border-bottom:0; }} .notice {{ padding:15px; border:1px solid #fed7aa; background:#fff7ed; border-radius:6px; }} ul {{ margin:10px 0; padding-left:20px; }} li {{ margin:8px 0; }} code {{ font-size:12px; color:#344054; word-break:break-all; }} footer {{ margin-top:30px; color:var(--muted); font-size:12px; }} @media (max-width:760px) {{ main {{ padding:22px 14px 40px; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} h1 {{ font-size:24px; }} }}
</style></head><body><main>
<header><h1>Relay Operations Dashboard</h1><p>UTC {html.escape(summary['window_start'])} to {html.escape(summary['window_end'])}</p></header>
<div class=\"warning\"><strong>Ledger boundary:</strong> Relay telemetry, Codex Rollout, and CC Switch are separate sources. This dashboard never adds their token totals.</div>
<div class=\"metrics\">{card_html}</div>
<section><h2>Relay Provider Health</h2><p>Version-filtered external production runs only.</p>{provider}</section>
{usage_section}
<section><h2>Reviewed Sources</h2><ul>{source_html}</ul></section>
<footer>Generated {html.escape(str(artifact['manifest']['generatedAt']))}. Relay effective tokens include only final strict-contract results with status success.</footer>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay-report", type=Path, required=True)
    parser.add_argument("--usage-audit", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--html-out", type=Path, help="Optional self-contained static dashboard output.")
    args = parser.parse_args()
    try:
        relay = read_object(args.relay_report, "Relay operational report")
        if relay.get("source") != "relay_operational_telemetry":
            raise ValueError("relay report has an unexpected source")
        usage = read_object(args.usage_audit, "usage audit") if args.usage_audit else None
        if usage is not None and not isinstance(usage.get("sources"), list):
            raise ValueError("usage audit has no source sections")
        artifact = build_artifact(relay, args.relay_report, usage, args.usage_audit)
    except ValueError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, separators=(",", ":")))
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    if args.html_out:
        args.html_out.parent.mkdir(parents=True, exist_ok=True)
        args.html_out.write_text(render_html(artifact), encoding="utf-8")
    print(json.dumps({"status": "success", "artifact": str(args.out), **({"html": str(args.html_out)} if args.html_out else {})}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
