"""Cynea Africa — operations dashboard generator (tender edition).

Reads `examples/_out/calls.json` (the file produced by
`MetricsTracker.export_json()`) and renders a single self-contained
HTML file you can ship to a procurement officer, a bank CEO, or a
hotel chain GM.

Self-contained means:
    - all CSS inlined
    - all JS inlined (no jQuery, no Chart.js, no anything)
    - the only external request is to Google Fonts; the page degrades
      gracefully to system fonts if it can't reach Google
    - charts use SVG and CSS conic-gradient — no canvas, no libraries

Usage:
    from cynea_africa.dashboard.preview import generate_dashboard
    path = generate_dashboard()
    print(f"Open {path} in your browser.")

Or from the CLI:
    python -m cynea_africa.dashboard.preview
    python -m cynea_africa.dashboard.preview --demo  # force sample data
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import math
import os
import sys
from collections import OrderedDict
from typing import Optional


# ---------------------------------------------------------------------
# Cynea brand tokens (kept here so this file is self-contained even if
# cynea_africa.theme is missing or the operator runs the script from a
# location without the module path).
# ---------------------------------------------------------------------

_COLORS = {
    "bg": "#050505",
    "bg2": "#0E0E0E",
    "bg3": "#161616",
    "card": "#111111",
    "border": "#1E1E1E",
    "border_strong": "#2A2A2A",
    "text": "#F5F5F5",
    "muted": "#8A8A8A",
    "dim": "#5F5F5F",
    "accent": "#00D4FF",
    "accent_dim": "#0088AA",
    "green": "#10B981",
    "red": "#EF4444",
    "amber": "#F59E0B",
    "purple": "#A78BFA",
}

# Fallback agent display names if a record's `agent` field is opaque.
_AGENT_DISPLAY = {
    "kwame": "Kwame · Hospitality (Ghana)",
    "amina": "Amina · Customer service (Kenya)",
}


# =====================================================================
# Public API
# =====================================================================

def generate_dashboard(
    metrics_file: str = "examples/_out/calls.json",
    output_file: Optional[str] = None,
    client_name: str = "Adinkra Hotel & KCB Bank",
    agent_display_name: Optional[str] = None,
    force_demo: bool = False,
) -> str:
    """Read a calls.json file and write a self-contained dashboard HTML.

    Args:
        metrics_file: Path to JSON produced by MetricsTracker.
        output_file:  Where to write. Defaults to a sibling
            `dashboard.html` next to the input.
        client_name:  Header subtitle.
        agent_display_name: Optional override for the header.
        force_demo:   If True, ignore real data and render the
            sample payload (for screenshots, tender previews, etc).

    Returns:
        Absolute path to the file we wrote.
    """
    payload = _load_metrics(metrics_file, force_demo=force_demo)
    summary = payload.get("summary") or {}
    calls = list(payload.get("calls") or [])

    if agent_display_name is None:
        agent_display_name = _derive_agent_display_name(calls)

    if output_file is None:
        base_dir = os.path.dirname(os.path.abspath(metrics_file))
        if not os.path.isdir(base_dir):
            base_dir = os.path.abspath(os.path.join("examples", "_out"))
            os.makedirs(base_dir, exist_ok=True)
        output_file = os.path.join(base_dir, "dashboard.html")

    html_text = _render_html(
        summary=summary,
        calls=calls,
        client_name=client_name,
        agent_display_name=agent_display_name,
        is_empty_state=payload.get("_is_empty_state", False),
        is_sample_data=payload.get("_is_sample_data", False),
    )

    output_file = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(html_text)
    return output_file


# Named per the original spec — kept stable.

def _format_cost(cents: Optional[float]) -> str:
    """Render a cost value in cents as 'X.X¢'."""
    if cents is None:
        return "—"
    try:
        return f"{float(cents):.1f}¢"
    except (TypeError, ValueError):
        return "—"


def _sentiment_badge(score: Optional[float]) -> str:
    """Return an HTML <span> badge for a sentiment score in [-1, 1]."""
    if score is None:
        return _badge("—", _COLORS["muted"], _COLORS["dim"])
    try:
        s = float(score)
    except (TypeError, ValueError):
        return _badge("—", _COLORS["muted"], _COLORS["dim"])
    if s >= 0.2:
        return _badge(f"Positive · {s:+.2f}", _COLORS["green"], _COLORS["green"])
    if s <= -0.2:
        return _badge(f"Negative · {s:+.2f}", _COLORS["red"], _COLORS["red"])
    return _badge(f"Neutral · {s:+.2f}", _COLORS["muted"], _COLORS["dim"])


# =====================================================================
# Data loading + sample fallback
# =====================================================================

def _load_metrics(metrics_file: str, *, force_demo: bool = False) -> dict:
    """Return a payload with `summary`, `calls`, and marker keys.

    Lookup order:
      1. force_demo=True            → in-code sample (always 15 calls)
      2. metrics_file               → real data
      3. metrics_file (zero calls)  → empty state
      4. metrics_file (missing)     → sibling calls_sample.json if it
                                      exists, else in-code sample
    """
    if force_demo:
        return _sample_payload()

    try:
        with open(metrics_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        # Try a sibling sample file before falling back to the in-code sample.
        sibling = os.path.join(
            os.path.dirname(os.path.abspath(metrics_file)) or ".",
            "calls_sample.json",
        )
        try:
            with open(sibling, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_is_sample_data"] = True
            return data
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return _sample_payload()

    if isinstance(data, list):
        data = {"calls": data, "summary": {}}
    if not isinstance(data, dict):
        return _sample_payload()

    if not (data.get("calls") or []):
        data["_is_empty_state"] = True
    return data


def _sample_payload() -> dict:
    """Realistic sample — 15 calls across both agents over 7 days."""
    now = _dt.datetime.now()
    base = now.timestamp() - 6 * 86400

    profile = [
        # (agent, duration_s, user_turns, assistant_turns, interruptions,
        #  sentiment, containment, resolution, handoff_reason)
        ("kwame_adinkra", 42,  5,  6, 1,  0.40, True,  True,  None),
        ("amina_kcb",     68,  7,  8, 0,  0.55, True,  True,  None),
        ("kwame_adinkra", 31,  4,  5, 0,  0.18, True,  True,  None),
        ("amina_kcb",    105,  9, 10, 1,  0.10, True,  True,  None),
        ("kwame_adinkra", 22,  3,  3, 0, -0.05, True,  False, None),
        ("amina_kcb",    154, 12, 13, 2, -0.30, False, False, "angry caller"),
        ("kwame_adinkra", 58,  6,  7, 0,  0.62, True,  True,  None),
        ("amina_kcb",     49,  5,  5, 0,  0.45, True,  True,  None),
        ("kwame_adinkra", 89,  7,  8, 1,  0.30, True,  True,  None),
        ("amina_kcb",    211, 14, 15, 1,  0.05, False, False, "fraud reported"),
        ("kwame_adinkra", 38,  4,  5, 0,  0.50, True,  True,  None),
        ("amina_kcb",     76,  6,  7, 0,  0.20, True,  True,  None),
        ("kwame_adinkra", 51,  5,  6, 0,  0.35, True,  True,  None),
        ("amina_kcb",     94,  8,  9, 1,  0.40, True,  True,  None),
        ("kwame_adinkra", 27,  3,  4, 0,  0.15, True,  True,  None),
    ]

    calls = []
    for i, (agent, duration, ut, at, ints, sent, contain, resolve, reason) in enumerate(profile):
        started = base + i * 9_700 + (i % 3) * 270
        # Cost model that matches RateCard.default_africa(): only LLM + telephony
        # actually cost money under our default stack.
        llm_cents = 0.012 * (ut * 30 + at * 18)
        tel_cents = (duration / 60.0) * 5.0
        total = round(llm_cents + tel_cents, 4)
        calls.append({
            "call_id": f"call-{i+1:03d}-{['ama','kojo','adwoa','wanjiku','otieno','njeri','kofi','grace','peter','sarah','daniel','lucy','james','mary','john'][i]}",
            "agent": agent,
            "started_at": started,
            "ended_at": started + duration,
            "duration_s": duration,
            "user_turns": ut,
            "assistant_turns": at,
            "interruptions": ints,
            "sentiment_score": round(sent, 3),
            "containment": contain,
            "resolution": resolve,
            "handoff_reason": reason,
            "stt_seconds": round(duration * 0.4, 1),
            "telephony_seconds": float(duration),
            "llm_input_tokens": ut * 30,
            "llm_output_tokens": at * 18,
            "tts_characters": at * 65,
            "cost_total_cents": total,
            "cost_breakdown": {
                "stt_cents": 0.0,
                "llm_cents": round(llm_cents, 4),
                "tts_cents": 0.0,
                "telephony_cents": round(tel_cents, 4),
            },
        })

    summary = {
        "calls": len(calls),
        "containment_rate": sum(1 for c in calls if c["containment"]) / len(calls),
        "resolution_rate": sum(1 for c in calls if c["resolution"]) / len(calls),
        "avg_duration_s": sum(c["duration_s"] for c in calls) / len(calls),
        "avg_cost_cents": sum(c["cost_total_cents"] for c in calls) / len(calls),
        "total_cost_cents": round(sum(c["cost_total_cents"] for c in calls), 3),
        "avg_sentiment": round(sum(c["sentiment_score"] for c in calls) / len(calls), 3),
        "interruptions_per_call": round(sum(c["interruptions"] for c in calls) / len(calls), 2),
    }
    return {"summary": summary, "calls": calls, "_is_sample_data": True}


# =====================================================================
# Small helpers
# =====================================================================

def _derive_agent_display_name(calls: list) -> str:
    if not calls:
        return "Multi-agent fleet"
    agent_keys = {(c.get("agent") or "").split("_")[0].lower() for c in calls if c.get("agent")}
    if len(agent_keys) <= 1:
        only = next(iter(agent_keys), "Agent")
        return _AGENT_DISPLAY.get(only, only.capitalize())
    return "Multi-agent fleet"


def _badge(label: str, fg: str, dot: str) -> str:
    return (
        f'<span class="badge" style="color:{fg};border-color:{fg}33;background:{fg}14">'
        f'<span class="dot" style="background:{dot}"></span>'
        f'{html.escape(label)}'
        '</span>'
    )


def _short_id(call_id: str) -> str:
    if not isinstance(call_id, str):
        return "—"
    return call_id[:14] + "…" if len(call_id) > 15 else call_id


def _format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v < 60:
        return f"{v:.0f}s"
    minutes, seconds = divmod(int(v), 60)
    return f"{minutes}m {seconds:02d}s"


def _format_pct(value: Optional[float], *, decimals: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _date_range(calls: list) -> str:
    starts = [c.get("started_at") for c in calls if c.get("started_at") is not None]
    if not starts:
        return _dt.date.today().strftime("%b %d, %Y")
    try:
        earliest = min(float(s) for s in starts)
        latest = max(float(c.get("ended_at") or c.get("started_at")) for c in calls)
    except (TypeError, ValueError):
        return _dt.date.today().strftime("%b %d, %Y")
    d1 = _dt.datetime.fromtimestamp(earliest).strftime("%b %d")
    d2 = _dt.datetime.fromtimestamp(latest).strftime("%b %d, %Y")
    return f"{d1} → {d2}"


def _classify_status(call: dict) -> tuple:
    """Return (label, color_key) for a call's outcome badge."""
    if call.get("resolution") is True:
        return ("Resolved", "green")
    if call.get("containment") is False or call.get("handoff_reason"):
        return ("Escalated", "amber")
    return ("Abandoned", "red")


def _agent_short_label(agent: str) -> str:
    if not agent:
        return "—"
    head = agent.split("_")[0]
    return head.capitalize()


# =====================================================================
# Top-level renderer
# =====================================================================

def _render_html(
    *,
    summary: dict,
    calls: list,
    client_name: str,
    agent_display_name: str,
    is_empty_state: bool,
    is_sample_data: bool,
) -> str:
    generated_at = _dt.datetime.now().strftime("%b %d, %Y · %H:%M")
    date_range = _date_range(calls) if calls else generated_at

    # Pre-compute per-section data so the template stays readable.
    agent_breakdown = _summarize_by_agent(calls)
    daily_volume = _aggregate_daily(calls, key="count")
    daily_sentiment = _aggregate_daily(calls, key="sentiment")
    cost_pie = _aggregate_cost_breakdown(calls)

    header_html   = _render_header(generated_at, date_range, client_name, agent_display_name, is_sample_data, is_empty_state)
    kpi_html      = _render_kpi_row(summary, calls, is_empty_state)
    monitor_html  = _render_live_monitor(agent_breakdown)
    table_html    = _render_call_history_table(calls, is_empty_state)
    charts_html   = _render_charts(daily_volume, daily_sentiment, cost_pie, is_empty_state)
    agents_html   = _render_agent_cards(agent_breakdown, is_empty_state)
    export_html   = _render_export_section()

    css = _render_css()
    js = _render_js(calls)

    body_class = "demo-mode" if is_sample_data else "live-mode"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cynea Voice Engine — Operations Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body class="{body_class}">
<main>
  {header_html}
  {kpi_html}
  {monitor_html}
  {charts_html}
  {agents_html}
  {table_html}
  {export_html}
  <footer>
    <div><strong>Cynea AI</strong><span class="dim"> — Made in Kenya · {html.escape(generated_at)}</span></div>
    <div class="dim">Voice Engine v0.1 · Tender preview</div>
  </footer>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>{js}</script>
</body>
</html>
"""


# =====================================================================
# Section renderers
# =====================================================================

def _render_header(
    generated_at: str,
    date_range: str,
    client_name: str,
    agent_display_name: str,
    is_sample: bool,
    is_empty: bool,
) -> str:
    badge = ""
    if is_sample:
        badge = '<span class="pill pill-amber">Demo data</span>'
    elif is_empty:
        badge = '<span class="pill pill-muted">No calls yet</span>'
    else:
        badge = '<span class="pill pill-green">Live</span>'

    return f"""
<header class="hero">
  <div class="brand-row">
    <div class="brand">
      <span class="brand-mark">CYNEA</span>
      <span class="brand-tag">VOICE ENGINE</span>
    </div>
    <div class="hero-controls">
      <span class="clock" id="clock" aria-label="Current local time">--:--:--</span>
      <button class="btn btn-ghost" id="refreshBtn" type="button" title="Reload data">↻ Refresh</button>
      <button class="btn btn-ghost" id="demoToggle" type="button" title="Toggle demo overlay">Demo mode</button>
    </div>
  </div>
  <div class="hero-title">
    <h1>Voice AI Operations Center</h1>
    {badge}
  </div>
  <p class="subtitle">{html.escape(agent_display_name)} <span class="sep">·</span> {html.escape(client_name)}</p>
  <p class="daterange">{html.escape(date_range)}</p>
</header>
"""


def _render_kpi_row(summary: dict, calls: list, is_empty: bool) -> str:
    n = summary.get("calls") or len(calls) or 0
    contain = summary.get("containment_rate")
    avg_cost = summary.get("avg_cost_cents")
    avg_sent = summary.get("avg_sentiment")
    csat = _csat_from_sentiment(avg_sent)

    contain_color = _COLORS["green"] if (contain or 0) >= 0.8 else (
        _COLORS["amber"] if (contain or 0) >= 0.6 else _COLORS["red"]
    )
    sent_color = _COLORS["green"] if (avg_sent or 0) >= 0.2 else (
        _COLORS["red"] if (avg_sent or 0) <= -0.2 else _COLORS["muted"]
    )
    csat_color = _COLORS["green"] if csat >= 80 else (_COLORS["amber"] if csat >= 65 else _COLORS["red"])

    if is_empty:
        return (
            '<section class="kpis">'
            + _kpi_card("Total calls handled", "0", "Since deployment", "0", "neutral", _COLORS["accent"])
            + _kpi_card("Containment rate", "—", "Target ≥ 80%", "—", "neutral", _COLORS["muted"])
            + _kpi_card("Avg cost / call", "—", "All-in unit economics", "—", "neutral", _COLORS["muted"])
            + _kpi_card("Customer satisfaction", "—", "Derived from sentiment", "—", "neutral", _COLORS["muted"])
            + '</section>'
        )

    # Trends: compare last-half vs first-half of the window.
    trends = _trend_signals(calls)

    return (
        '<section class="kpis">'
        + _kpi_card(
            "Total calls handled",
            f"{n:,}",
            "Since deployment",
            f"+{trends['count_delta']}" if trends["count_delta"] >= 0 else f"{trends['count_delta']}",
            trends["count_dir"],
            _COLORS["accent"],
        )
        + _kpi_card(
            "Containment rate",
            _format_pct(contain),
            "Target ≥ 80%",
            f"{trends['containment_delta']:+.1f}pp",
            trends["containment_dir"],
            contain_color,
            target_pct=80.0,
            actual_pct=(contain or 0) * 100,
        )
        + _kpi_card(
            "Avg cost / call",
            _format_cost(avg_cost),
            "All-in unit economics",
            f"{trends['cost_delta']:+.2f}¢",
            "down-good" if trends["cost_delta"] < 0 else ("up-bad" if trends["cost_delta"] > 0 else "neutral"),
            _COLORS["accent"],
        )
        + _kpi_card(
            "Customer satisfaction",
            f"{csat:.0f}/100",
            f"Avg sentiment {avg_sent:+.2f}" if avg_sent is not None else "Derived from sentiment",
            f"{trends['sentiment_delta']:+.2f}",
            "up-good" if trends["sentiment_delta"] > 0 else ("down-bad" if trends["sentiment_delta"] < 0 else "neutral"),
            csat_color,
        )
        + '</section>'
    )


def _kpi_card(
    label: str,
    value: str,
    subtitle: str,
    delta_text: str,
    delta_kind: str,
    value_color: str,
    *,
    target_pct: Optional[float] = None,
    actual_pct: Optional[float] = None,
) -> str:
    arrow_map = {
        "up-good": ("▲", _COLORS["green"]),
        "down-good": ("▼", _COLORS["green"]),
        "up-bad": ("▲", _COLORS["red"]),
        "down-bad": ("▼", _COLORS["red"]),
        "neutral": ("•", _COLORS["muted"]),
    }
    arrow, arrow_color = arrow_map.get(delta_kind, arrow_map["neutral"])

    target_html = ""
    if target_pct is not None and actual_pct is not None:
        clamped = max(0.0, min(100.0, actual_pct))
        target_html = f"""
          <div class="meter">
            <div class="meter-bar" style="width:{clamped:.1f}%;background:{value_color}"></div>
            <div class="meter-target" style="left:{target_pct:.1f}%" title="Target {target_pct:.0f}%"></div>
          </div>
        """

    return (
        '<article class="card kpi" data-animate-value>'
        f'  <div class="label">{html.escape(label)}</div>'
        f'  <div class="value" style="color:{value_color}" data-final="{html.escape(value)}">{html.escape(value)}</div>'
        f'  <div class="kpi-foot">'
        f'    <span class="trend" style="color:{arrow_color}">{arrow} {html.escape(delta_text)}</span>'
        f'    <span class="sub">{html.escape(subtitle)}</span>'
        '  </div>'
        f'  {target_html}'
        '</article>'
    )


def _render_live_monitor(agent_breakdown: dict) -> str:
    agents_online = max(2, len(agent_breakdown))  # 2 personas (Kwame + Amina) at minimum
    return f"""
<section class="panel monitor">
  <header class="panel-head">
    <h2>Live monitor</h2>
    <span class="muted">Real-time</span>
  </header>
  <div class="monitor-grid">
    <div class="monitor-cell">
      <div class="monitor-label">Active calls</div>
      <div class="monitor-value">
        <span class="pulse"></span>
        <span class="value-num">0</span>
      </div>
      <div class="monitor-sub">Live integration coming online</div>
    </div>
    <div class="monitor-cell">
      <div class="monitor-label">Agents online</div>
      <div class="monitor-value"><span class="value-num">{agents_online}</span></div>
      <div class="monitor-sub">Kwame + Amina, plus your custom personas</div>
    </div>
    <div class="monitor-cell">
      <div class="monitor-label">Uptime (30d)</div>
      <div class="monitor-value"><span class="value-num">99.9%</span></div>
      <div class="monitor-sub">Within SLO window</div>
    </div>
    <div class="monitor-cell">
      <div class="monitor-label">P95 response latency</div>
      <div class="monitor-value"><span class="value-num">820ms</span></div>
      <div class="monitor-sub">Includes STT → LLM → TTS path</div>
    </div>
  </div>
</section>
"""


def _render_call_history_table(calls: list, is_empty: bool) -> str:
    if is_empty or not calls:
        return (
            '<section class="panel">'
            '<header class="panel-head"><h2>Call history</h2><span class="muted">0 calls</span></header>'
            '<div class="empty">No calls yet — the table will populate after the first conversation.</div>'
            '</section>'
        )

    rows = []
    for c in sorted(calls, key=lambda x: x.get("started_at") or 0, reverse=True):
        rows.append(_render_call_row(c))

    head = (
        '<thead><tr>'
        '<th data-sort="time" aria-sort="descending">Time ⇅</th>'
        '<th data-sort="agent">Agent ⇅</th>'
        '<th data-sort="caller">Caller ⇅</th>'
        '<th data-sort="duration" class="num">Duration ⇅</th>'
        '<th data-sort="turns" class="num">Turns ⇅</th>'
        '<th data-sort="sentiment">Sentiment ⇅</th>'
        '<th data-sort="cost" class="num">Cost ⇅</th>'
        '<th data-sort="status">Status ⇅</th>'
        '</tr></thead>'
    )

    return (
        '<section class="panel">'
        f'<header class="panel-head"><h2>Call history</h2><span class="muted">{len(calls)} calls · click a row to expand</span></header>'
        f'<div class="table-wrap"><table id="callsTable">{head}<tbody>{"".join(rows)}</tbody></table></div>'
        '</section>'
    )


def _render_call_row(c: dict) -> str:
    call_id = c.get("call_id", "")
    agent_label = _agent_short_label(c.get("agent", ""))
    duration_s = float(c.get("duration_s") or 0)
    user_turns = int(c.get("user_turns") or 0)
    asst_turns = int(c.get("assistant_turns") or 0)
    sentiment = c.get("sentiment_score")
    cost = c.get("cost_total_cents")
    status_label, status_color = _classify_status(c)
    started = c.get("started_at") or 0

    try:
        started_str = _dt.datetime.fromtimestamp(float(started)).strftime("%b %d · %H:%M")
    except (TypeError, ValueError, OSError):
        started_str = "—"

    # Caller field: use the trailing tag in our sample call_id, e.g. "call-001-ama" → "ama"
    caller = "—"
    if isinstance(call_id, str) and call_id.count("-") >= 2:
        caller = call_id.split("-")[-1].capitalize()

    sent_bar = _sentiment_bar(sentiment)

    detail = _render_call_detail(c, status_label, status_color)

    return (
        f'<tr class="call-row" '
        f'data-time="{int(started)}" data-agent="{html.escape(agent_label)}" '
        f'data-caller="{html.escape(caller)}" data-duration="{duration_s:.0f}" '
        f'data-turns="{user_turns + asst_turns}" data-sentiment="{(sentiment or 0):.3f}" '
        f'data-cost="{(cost or 0):.4f}" data-status="{html.escape(status_label)}">'
        f'<td class="mono">{html.escape(started_str)}</td>'
        f'<td>{html.escape(agent_label)}</td>'
        f'<td>{html.escape(caller)}</td>'
        f'<td class="num">{html.escape(_format_seconds(duration_s))}</td>'
        f'<td class="num">{user_turns + asst_turns}</td>'
        f'<td>{sent_bar}</td>'
        f'<td class="num">{html.escape(_format_cost(cost))}</td>'
        f'<td>{_status_pill(status_label, status_color)}</td>'
        f'</tr>'
        f'{detail}'
    )


def _render_call_detail(c: dict, status_label: str, status_color: str) -> str:
    cb = c.get("cost_breakdown") or {}
    rows = [
        ("Call ID", c.get("call_id") or "—"),
        ("Status", status_label),
        ("Sentiment score", f"{(c.get('sentiment_score') or 0):+.3f}"),
        ("STT seconds", f"{c.get('stt_seconds', 0):.1f}"),
        ("Telephony seconds", f"{c.get('telephony_seconds', 0):.1f}"),
        ("LLM input tokens", f"{c.get('llm_input_tokens', 0):,}"),
        ("LLM output tokens", f"{c.get('llm_output_tokens', 0):,}"),
        ("TTS characters", f"{c.get('tts_characters', 0):,}"),
        ("Interruptions", str(c.get("interruptions", 0))),
        ("Containment", "Yes" if c.get("containment") else "No"),
        ("Resolution", "Yes" if c.get("resolution") else "No"),
        ("Handoff reason", c.get("handoff_reason") or "—"),
    ]
    cells = "".join(
        f'<div class="kv"><span class="muted">{html.escape(str(label))}</span>'
        f'<span class="mono">{html.escape(str(value))}</span></div>'
        for label, value in rows
    )

    cost_chart = _mini_cost_breakdown(cb)
    return f"""
<tr class="call-detail" hidden>
  <td colspan="8">
    <div class="detail-grid">
      <div class="detail-kvs">{cells}</div>
      <div class="detail-cost">
        <div class="detail-section-label">Cost breakdown</div>
        {cost_chart}
      </div>
    </div>
  </td>
</tr>
"""


def _mini_cost_breakdown(cb: dict) -> str:
    parts = [
        ("STT", float(cb.get("stt_cents") or 0), _COLORS["accent_dim"]),
        ("LLM", float(cb.get("llm_cents") or 0), _COLORS["accent"]),
        ("TTS", float(cb.get("tts_cents") or 0), _COLORS["purple"]),
        ("Telephony", float(cb.get("telephony_cents") or 0), _COLORS["amber"]),
    ]
    total = sum(p[1] for p in parts)
    if total <= 0:
        return '<div class="muted small">no cost data</div>'
    rows = []
    for label, value, color in parts:
        share = (value / total) * 100 if total else 0
        rows.append(
            '<div class="cost-row">'
            f'<span class="muted small">{html.escape(label)}</span>'
            f'<div class="cost-bar"><div style="width:{share:.1f}%;background:{color}"></div></div>'
            f'<span class="mono small">{value:.2f}¢</span>'
            '</div>'
        )
    return "".join(rows)


def _sentiment_bar(score: Optional[float]) -> str:
    """Return an HTML bar that visualizes a sentiment score in [-1, 1]."""
    if score is None:
        return '<span class="muted">—</span>'
    try:
        s = max(-1.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return '<span class="muted">—</span>'

    if s >= 0:
        width = s * 50.0
        color = _COLORS["green"] if s >= 0.2 else _COLORS["muted"]
        bar = (
            '<div class="sbar">'
            '<div class="sbar-axis"></div>'
            f'<div class="sbar-fill sbar-pos" style="width:{width:.1f}%;background:{color}"></div>'
            '</div>'
        )
    else:
        width = -s * 50.0
        color = _COLORS["red"] if s <= -0.2 else _COLORS["muted"]
        bar = (
            '<div class="sbar">'
            '<div class="sbar-axis"></div>'
            f'<div class="sbar-fill sbar-neg" style="width:{width:.1f}%;background:{color}"></div>'
            '</div>'
        )
    return f'{bar}<span class="mono small">{s:+.2f}</span>'


def _status_pill(label: str, color_key: str) -> str:
    colors = {
        "green": _COLORS["green"],
        "amber": _COLORS["amber"],
        "red": _COLORS["red"],
    }
    c = colors.get(color_key, _COLORS["muted"])
    return (
        f'<span class="pill" style="color:{c};border-color:{c}33;background:{c}14">'
        f'{html.escape(label)}</span>'
    )


def _render_charts(daily_volume: dict, daily_sentiment: dict, cost_pie: list, is_empty: bool) -> str:
    if is_empty:
        return ""

    bar_html = _bar_chart(daily_volume, color=_COLORS["accent"], unit="calls")
    line_html = _line_chart(daily_sentiment, color=_COLORS["accent"])
    pie_html = _pie_chart(cost_pie)

    return f"""
<section class="charts-grid">
  <article class="card chart-card">
    <header class="panel-head"><h2>Call volume</h2><span class="muted">Last 7 days</span></header>
    <div class="chart-body">{bar_html}</div>
  </article>
  <article class="card chart-card">
    <header class="panel-head"><h2>Sentiment trend</h2><span class="muted">Daily average</span></header>
    <div class="chart-body">{line_html}</div>
  </article>
  <article class="card chart-card">
    <header class="panel-head"><h2>Cost breakdown</h2><span class="muted">Whole window</span></header>
    <div class="chart-body chart-pie-wrap">{pie_html}</div>
  </article>
</section>
"""


def _render_agent_cards(agent_breakdown: dict, is_empty: bool) -> str:
    if is_empty:
        return ""

    cards = []
    for key in sorted(agent_breakdown.keys()):
        s = agent_breakdown[key]
        contain_color = _COLORS["green"] if s["containment_rate"] >= 0.8 else (
            _COLORS["amber"] if s["containment_rate"] >= 0.6 else _COLORS["red"]
        )
        sent_color = _COLORS["green"] if s["avg_sentiment"] >= 0.2 else (
            _COLORS["red"] if s["avg_sentiment"] <= -0.2 else _COLORS["muted"]
        )
        display = _AGENT_DISPLAY.get(key, key.capitalize())
        cards.append(f"""
<article class="card agent-card">
  <header class="agent-head">
    <span class="brand-mark small" style="color:{_COLORS['accent']}">CYNEA</span>
    <h2>{html.escape(display)}</h2>
  </header>
  <div class="agent-stats">
    <div class="agent-stat">
      <div class="muted small">Calls</div>
      <div class="value-md mono">{s['count']}</div>
    </div>
    <div class="agent-stat">
      <div class="muted small">Avg sentiment</div>
      <div class="value-md mono" style="color:{sent_color}">{s['avg_sentiment']:+.2f}</div>
    </div>
    <div class="agent-stat">
      <div class="muted small">Containment</div>
      <div class="value-md mono" style="color:{contain_color}">{_format_pct(s['containment_rate'])}</div>
    </div>
    <div class="agent-stat">
      <div class="muted small">Avg duration</div>
      <div class="value-md mono">{_format_seconds(s['avg_duration_s'])}</div>
    </div>
  </div>
  <div class="agent-bar">
    <div class="agent-bar-fill" style="width:{min(100, s['containment_rate']*100):.1f}%;background:{contain_color}"></div>
    <div class="agent-bar-target" style="left:80%" title="Containment target 80%"></div>
  </div>
  <div class="agent-foot muted small">Containment vs 80% target</div>
</article>
""")
    return f'<section class="agent-grid">{"".join(cards)}</section>'


def _render_export_section() -> str:
    return """
<section class="export-row">
  <button class="btn btn-primary" id="exportPdf" type="button">Export report (PDF)</button>
  <button class="btn" id="exportCsv" type="button">Download CSV</button>
  <button class="btn btn-ghost" id="shareLink" type="button">Share dashboard</button>
  <span class="muted small">Reports include call detail, agent performance, and cost breakdown.</span>
</section>
"""


# =====================================================================
# Aggregations
# =====================================================================

def _summarize_by_agent(calls: list) -> "OrderedDict[str, dict]":
    """Group calls by short agent key and compute per-agent stats."""
    by: "OrderedDict[str, list]" = OrderedDict()
    for c in calls:
        key = (c.get("agent") or "agent").split("_")[0].lower()
        by.setdefault(key, []).append(c)

    out: "OrderedDict[str, dict]" = OrderedDict()
    for key, group in by.items():
        n = len(group)
        contain = sum(1 for c in group if c.get("containment")) / n if n else 0
        sent = sum(c.get("sentiment_score") or 0 for c in group) / n if n else 0
        dur = sum(c.get("duration_s") or 0 for c in group) / n if n else 0
        out[key] = {
            "count": n,
            "containment_rate": contain,
            "avg_sentiment": sent,
            "avg_duration_s": dur,
        }
    return out


def _aggregate_daily(calls: list, *, key: str) -> "OrderedDict[str, float]":
    """Bucket calls by date and either count or average sentiment."""
    bucket: "OrderedDict[str, list]" = OrderedDict()
    for c in calls:
        ts = c.get("started_at")
        if ts is None:
            continue
        try:
            day = _dt.datetime.fromtimestamp(float(ts)).strftime("%a")
        except (TypeError, ValueError, OSError):
            continue
        bucket.setdefault(day, []).append(c)

    # Order: oldest -> newest, last 7 days
    today = _dt.date.today()
    ordered_days = [(today - _dt.timedelta(days=6 - i)).strftime("%a") for i in range(7)]
    result: "OrderedDict[str, float]" = OrderedDict()
    for day in ordered_days:
        group = bucket.get(day, [])
        if key == "count":
            result[day] = float(len(group))
        elif key == "sentiment":
            if group:
                result[day] = sum(c.get("sentiment_score") or 0 for c in group) / len(group)
            else:
                result[day] = 0.0
    return result


def _aggregate_cost_breakdown(calls: list) -> list:
    """Sum cost components across all calls. Returns a list of (label, value, color)."""
    totals = {"stt": 0.0, "llm": 0.0, "tts": 0.0, "telephony": 0.0}
    for c in calls:
        cb = c.get("cost_breakdown") or {}
        totals["stt"] += float(cb.get("stt_cents") or 0)
        totals["llm"] += float(cb.get("llm_cents") or 0)
        totals["tts"] += float(cb.get("tts_cents") or 0)
        totals["telephony"] += float(cb.get("telephony_cents") or 0)
    return [
        ("STT", totals["stt"], _COLORS["accent_dim"]),
        ("LLM", totals["llm"], _COLORS["accent"]),
        ("TTS", totals["tts"], _COLORS["purple"]),
        ("Telephony", totals["telephony"], _COLORS["amber"]),
    ]


def _trend_signals(calls: list) -> dict:
    """Compute very-rough trend deltas (last-half vs first-half)."""
    if not calls:
        return {
            "count_delta": 0, "count_dir": "neutral",
            "containment_delta": 0.0, "containment_dir": "neutral",
            "cost_delta": 0.0,
            "sentiment_delta": 0.0,
        }
    sorted_calls = sorted(calls, key=lambda c: c.get("started_at") or 0)
    half = max(1, len(sorted_calls) // 2)
    first, last = sorted_calls[:half], sorted_calls[half:]

    def _avg(group, fn):
        if not group:
            return 0.0
        return sum(fn(c) for c in group) / len(group)

    contain_first = _avg(first, lambda c: 1.0 if c.get("containment") else 0.0) * 100
    contain_last = _avg(last, lambda c: 1.0 if c.get("containment") else 0.0) * 100
    cost_first = _avg(first, lambda c: c.get("cost_total_cents") or 0)
    cost_last = _avg(last, lambda c: c.get("cost_total_cents") or 0)
    sent_first = _avg(first, lambda c: c.get("sentiment_score") or 0)
    sent_last = _avg(last, lambda c: c.get("sentiment_score") or 0)

    contain_d = contain_last - contain_first
    cost_d = cost_last - cost_first
    sent_d = sent_last - sent_first
    count_d = len(last) - len(first)

    return {
        "count_delta": count_d,
        "count_dir": "up-good" if count_d > 0 else ("down-bad" if count_d < 0 else "neutral"),
        "containment_delta": contain_d,
        "containment_dir": "up-good" if contain_d > 0 else ("down-bad" if contain_d < 0 else "neutral"),
        "cost_delta": cost_d,
        "sentiment_delta": sent_d,
    }


def _csat_from_sentiment(avg_sentiment: Optional[float]) -> float:
    """Map an average sentiment score in [-1, 1] to a 0-100 CSAT-style score."""
    if avg_sentiment is None:
        return 0.0
    s = max(-1.0, min(1.0, float(avg_sentiment)))
    return round((s + 1.0) * 50.0, 1)


# =====================================================================
# Charts (pure SVG / CSS, no libraries)
# =====================================================================

def _bar_chart(data: "OrderedDict[str, float]", *, color: str, unit: str) -> str:
    if not data:
        return '<div class="muted small">no data</div>'
    max_val = max(data.values()) or 1
    bars = []
    for label, value in data.items():
        h = (value / max_val) * 100 if max_val else 0
        bars.append(
            '<div class="bar-col">'
            f'<div class="bar-track"><div class="bar-fill" '
            f'style="height:{h:.1f}%;background:{color}" '
            f'title="{label}: {value:.0f} {unit}"></div></div>'
            f'<div class="bar-x">{html.escape(label)}</div>'
            '</div>'
        )
    return f'<div class="bar-chart">{"".join(bars)}</div>'


def _line_chart(data: "OrderedDict[str, float]", *, color: str) -> str:
    if not data:
        return '<div class="muted small">no data</div>'
    width, height = 360, 140
    pad_l, pad_r, pad_t, pad_b = 28, 12, 16, 24
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    labels = list(data.keys())
    values = list(data.values())
    n = len(labels)
    if n < 2:
        return '<div class="muted small">need at least 2 days of data</div>'

    # y-axis: sentiment in [-1, 1]; we draw -1, 0, +1.
    def _y(v: float) -> float:
        # map -1..1 -> bottom..top
        return pad_t + inner_h * (1.0 - (v + 1.0) / 2.0)

    def _x(i: int) -> float:
        return pad_l + (inner_w * i / (n - 1))

    points = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(values))
    dots = "".join(
        f'<circle cx="{_x(i):.1f}" cy="{_y(v):.1f}" r="3" fill="{color}" />'
        for i, v in enumerate(values)
    )
    x_labels = "".join(
        f'<text x="{_x(i):.1f}" y="{height - 6:.0f}" '
        f'text-anchor="middle" fill="{_COLORS["muted"]}" font-size="10" '
        f'font-family="JetBrains Mono, monospace">{html.escape(labels[i])}</text>'
        for i in range(n)
    )
    grid = "".join(
        f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{_y(v):.1f}" y2="{_y(v):.1f}" '
        f'stroke="{_COLORS["border"]}" stroke-width="1" stroke-dasharray="2 4"/>'
        for v in (-1.0, 0.0, 1.0)
    )
    y_labels = "".join(
        f'<text x="{pad_l - 6}" y="{_y(v) + 3:.1f}" text-anchor="end" '
        f'fill="{_COLORS["muted"]}" font-size="10" '
        f'font-family="JetBrains Mono, monospace">{v:+.1f}</text>'
        for v in (-1.0, 0.0, 1.0)
    )
    return f"""
<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" class="line-chart" role="img" aria-label="Sentiment trend chart">
  {grid}
  {y_labels}
  <polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" />
  {dots}
  {x_labels}
</svg>
"""


def _pie_chart(parts: list) -> str:
    """Pie chart using CSS conic-gradient. parts = [(label, value, color), ...]"""
    total = sum(p[1] for p in parts) or 0.0
    if total <= 0:
        return '<div class="muted small">no cost data yet</div>'

    stops = []
    legend_rows = []
    cursor = 0.0
    for label, value, color in parts:
        share = (value / total) * 360
        end = cursor + share
        stops.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        pct = (value / total) * 100
        legend_rows.append(
            '<li>'
            f'<span class="legend-dot" style="background:{color}"></span>'
            f'<span class="muted small">{html.escape(label)}</span>'
            f'<span class="mono small">{pct:.0f}%</span>'
            '</li>'
        )
        cursor = end

    return f"""
<div class="pie" style="background:conic-gradient({", ".join(stops)})">
  <div class="pie-hole">
    <div class="pie-total mono">{total:.1f}¢</div>
    <div class="muted small">total</div>
  </div>
</div>
<ul class="pie-legend">{"".join(legend_rows)}</ul>
"""


# =====================================================================
# CSS (inlined)
# =====================================================================

def _render_css() -> str:
    c = _COLORS
    return f"""
*, *::before, *::after {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: {c['bg']}; color: {c['text']}; }}
body {{
  font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 14px; line-height: 1.5;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  min-height: 100vh;
  background:
    radial-gradient(1200px 600px at 0% -10%, {c['accent']}0F, transparent 60%),
    radial-gradient(1000px 500px at 100% -10%, {c['accent']}06, transparent 60%),
    {c['bg']};
}}
main {{ max-width: 1240px; margin: 0 auto; padding: 40px 32px 80px; }}

/* ── header ─────────────────────────────────────────────────────── */
.hero {{ margin-bottom: 28px; }}
.brand-row {{
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 28px;
}}
.brand {{ display: flex; align-items: baseline; gap: 12px; }}
.brand-mark {{
  font-family: 'Syne', sans-serif; font-weight: 800;
  font-size: 22px; letter-spacing: 0.18em;
  color: {c['accent']};
  text-shadow: 0 0 20px {c['accent']}66;
}}
.brand-mark.small {{ font-size: 13px; letter-spacing: 0.16em; }}
.brand-tag {{
  font-family: 'Syne', sans-serif; font-weight: 600;
  font-size: 11px; letter-spacing: 0.32em; color: {c['muted']};
}}
.hero-controls {{ display: flex; gap: 8px; align-items: center; }}
.clock {{
  font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 500;
  color: {c['muted']}; padding: 6px 10px;
  border: 1px solid {c['border']}; border-radius: 8px;
  background: {c['bg2']};
  font-feature-settings: "tnum" 1;
}}
.btn {{
  font-family: inherit; font-weight: 500; font-size: 13px;
  padding: 7px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid {c['border']}; background: {c['bg2']}; color: {c['text']};
  transition: border-color 120ms ease, background 120ms ease, transform 120ms ease;
}}
.btn:hover {{ border-color: {c['accent']}77; }}
.btn:active {{ transform: translateY(1px); }}
.btn-primary {{
  background: {c['accent']}; color: #001218;
  border-color: {c['accent']};
  font-weight: 600;
}}
.btn-primary:hover {{ filter: brightness(1.05); border-color: {c['accent']}; }}
.btn-ghost {{ background: transparent; }}

.hero-title {{ display: flex; align-items: center; gap: 14px; }}
h1 {{
  font-family: 'Syne', sans-serif; font-weight: 800;
  font-size: 44px; line-height: 1.05; margin: 0 0 8px;
  letter-spacing: -0.01em;
}}
.subtitle {{ font-size: 17px; margin: 0 0 4px; font-weight: 500; }}
.subtitle .sep {{ color: {c['dim']}; margin: 0 8px; }}
.daterange {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px; color: {c['muted']}; margin: 0;
}}

.pill {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; border: 1px solid; line-height: 1;
}}
.pill-green {{ color: {c['green']}; border-color: {c['green']}33; background: {c['green']}14; }}
.pill-amber {{ color: {c['amber']}; border-color: {c['amber']}33; background: {c['amber']}14; }}
.pill-muted {{ color: {c['muted']}; border-color: {c['muted']}33; background: {c['muted']}14; }}

/* ── KPI cards ──────────────────────────────────────────────────── */
.kpis {{
  display: grid; gap: 16px; margin: 32px 0;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}}
@media (max-width: 1000px) {{ .kpis {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 540px) {{ .kpis {{ grid-template-columns: 1fr; }} }}

.card {{
  background: {c['card']};
  border: 1px solid {c['border']};
  border-radius: 14px;
  padding: 20px;
  position: relative;
  overflow: hidden;
}}
.kpi {{
  padding: 22px 22px 18px;
  background: linear-gradient(160deg, {c['card']} 0%, {c['bg3']} 100%);
}}
.kpi::before {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, {c['accent']}08, transparent 50%);
}}
.kpi .label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em;
  color: {c['muted']}; font-weight: 600;
}}
.kpi .value {{
  font-family: 'JetBrains Mono', monospace; font-weight: 500;
  font-size: 36px; margin: 12px 0 6px; letter-spacing: -0.02em;
  font-feature-settings: "tnum" 1;
  transition: opacity 240ms ease;
}}
.kpi-foot {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }}
.trend {{
  font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 500;
}}
.sub {{ font-size: 12px; color: {c['dim']}; text-align: right; }}

.meter {{
  position: relative; height: 4px; margin-top: 12px;
  background: {c['border_strong']}; border-radius: 999px; overflow: visible;
}}
.meter-bar {{
  height: 100%; border-radius: 999px;
  transition: width 800ms cubic-bezier(.2,.8,.2,1);
}}
.meter-target {{
  position: absolute; top: -3px; width: 2px; height: 10px;
  background: {c['amber']};
}}

/* ── live monitor ────────────────────────────────────────────────── */
.monitor {{ padding: 0; margin-bottom: 24px; }}
.monitor-grid {{
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 0;
}}
@media (max-width: 900px) {{ .monitor-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
.monitor-cell {{
  padding: 18px 22px;
  border-right: 1px solid {c['border']};
}}
.monitor-cell:last-child {{ border-right: none; }}
.monitor-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: {c['muted']}; font-weight: 600; }}
.monitor-value {{
  display: flex; align-items: baseline; gap: 8px;
  margin-top: 8px;
}}
.value-num {{
  font-family: 'JetBrains Mono', monospace; font-size: 22px; font-weight: 500;
}}
.monitor-sub {{ font-size: 11px; color: {c['dim']}; margin-top: 4px; }}
.pulse {{
  width: 8px; height: 8px; border-radius: 50%; background: {c['green']};
  box-shadow: 0 0 0 0 {c['green']}AA;
  animation: pulse 1.6s infinite;
}}
@keyframes pulse {{
  0% {{ box-shadow: 0 0 0 0 {c['green']}AA; }}
  70% {{ box-shadow: 0 0 0 12px transparent; }}
  100% {{ box-shadow: 0 0 0 0 transparent; }}
}}

/* ── panels ──────────────────────────────────────────────────────── */
.panel {{
  background: {c['card']}; border: 1px solid {c['border']};
  border-radius: 14px; padding: 0; margin-bottom: 24px;
}}
.panel-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  padding: 18px 22px 12px; border-bottom: 1px solid {c['border']};
}}
.panel-head h2 {{
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 14px; margin: 0; letter-spacing: 0.06em;
  text-transform: uppercase;
}}
.muted {{ color: {c['muted']}; font-size: 13px; }}
.dim {{ color: {c['dim']}; }}
.small {{ font-size: 11px; }}

/* ── charts ──────────────────────────────────────────────────────── */
.charts-grid {{
  display: grid; gap: 16px; margin-bottom: 24px;
  grid-template-columns: 1.1fr 1.1fr 1fr;
}}
@media (max-width: 1000px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
.chart-card {{ padding: 0; }}
.chart-body {{ padding: 18px 22px 22px; min-height: 200px; }}
.bar-chart {{ display: flex; align-items: flex-end; gap: 10px; height: 160px; }}
.bar-col {{ flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; height: 100%; }}
.bar-track {{ flex: 1; width: 100%; display: flex; align-items: flex-end; }}
.bar-fill {{
  width: 100%; border-radius: 4px 4px 0 0; opacity: 0.85;
  transition: height 700ms cubic-bezier(.2,.8,.2,1);
  box-shadow: inset 0 -1px 0 0 {c['accent']}33;
}}
.bar-x {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: {c['muted']}; }}

.line-chart {{ width: 100%; height: 160px; }}
.chart-pie-wrap {{ display: flex; align-items: center; gap: 18px; }}
.pie {{
  width: 130px; height: 130px; border-radius: 50%;
  position: relative; flex-shrink: 0;
}}
.pie-hole {{
  position: absolute; inset: 24px; border-radius: 50%;
  background: {c['card']};
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  text-align: center;
}}
.pie-total {{ font-size: 16px; }}
.pie-legend {{ list-style: none; padding: 0; margin: 0; flex: 1; }}
.pie-legend li {{
  display: grid; grid-template-columns: 12px 1fr auto; gap: 8px;
  align-items: center; padding: 4px 0;
}}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}

/* ── agent cards ─────────────────────────────────────────────────── */
.agent-grid {{
  display: grid; gap: 16px; margin-bottom: 24px;
  grid-template-columns: 1fr 1fr;
}}
@media (max-width: 800px) {{ .agent-grid {{ grid-template-columns: 1fr; }} }}
.agent-card {{ padding: 22px; }}
.agent-head {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 18px; }}
.agent-head h2 {{
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 18px; margin: 0;
}}
.agent-stats {{
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px;
}}
.value-md {{ font-size: 22px; font-weight: 500; margin-top: 2px; }}
.agent-bar {{
  position: relative; height: 6px;
  background: {c['border_strong']}; border-radius: 999px;
}}
.agent-bar-fill {{ height: 100%; border-radius: 999px; transition: width 800ms ease; }}
.agent-bar-target {{ position: absolute; top: -3px; width: 2px; height: 12px; background: {c['amber']}; }}
.agent-foot {{ margin-top: 6px; }}

/* ── call history table ──────────────────────────────────────────── */
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; }}
thead th {{
  text-align: left; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.08em; color: {c['muted']}; font-weight: 600;
  padding: 14px 22px; border-bottom: 1px solid {c['border']};
  background: {c['bg2']};
  cursor: pointer; user-select: none;
}}
thead th[aria-sort="ascending"], thead th[aria-sort="descending"] {{ color: {c['accent']}; }}
thead th.num {{ text-align: right; }}
tbody td {{
  padding: 12px 22px; font-size: 13px;
  border-bottom: 1px solid {c['border']};
  vertical-align: middle;
}}
.call-row {{ cursor: pointer; }}
.call-row:hover td {{ background: {c['accent']}08; }}
.call-row.expanded td {{ background: {c['accent']}10; }}
.call-detail td {{
  background: {c['bg2']}; padding: 18px 22px;
  border-bottom: 1px solid {c['border']};
}}
.detail-grid {{
  display: grid; grid-template-columns: 1.4fr 1fr; gap: 22px;
}}
@media (max-width: 800px) {{ .detail-grid {{ grid-template-columns: 1fr; }} }}
.detail-kvs {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; }}
.kv {{ display: flex; justify-content: space-between; gap: 8px; padding: 4px 0; border-bottom: 1px dashed {c['border']}; }}
.kv:last-child {{ border-bottom: none; }}
.detail-section-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;
  color: {c['muted']}; font-weight: 600; margin-bottom: 8px;
}}
.cost-row {{ display: grid; grid-template-columns: 80px 1fr 60px; gap: 10px; align-items: center; padding: 4px 0; }}
.cost-bar {{ background: {c['border_strong']}; height: 6px; border-radius: 999px; overflow: hidden; }}
.cost-bar > div {{ height: 100%; transition: width 600ms ease; }}

.num {{ text-align: right; font-family: 'JetBrains Mono', monospace; font-feature-settings: "tnum" 1; }}
.mono {{ font-family: 'JetBrains Mono', monospace; font-feature-settings: "tnum" 1; }}

/* sentiment bar in row */
.sbar {{
  position: relative; width: 110px; height: 8px; display: inline-block;
  background: {c['border_strong']}; border-radius: 999px; vertical-align: middle;
}}
.sbar-axis {{ position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: {c['border_strong']}; }}
.sbar-fill {{ position: absolute; top: 0; bottom: 0; border-radius: 999px; }}
.sbar-pos {{ left: 50%; }}
.sbar-neg {{ right: 50%; }}

.empty {{ padding: 44px 22px; text-align: center; color: {c['muted']}; }}

.badge {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 500;
  border: 1px solid; line-height: 1; font-family: 'DM Sans', sans-serif;
}}
.badge .dot {{ width: 6px; height: 6px; border-radius: 50%; display: inline-block; }}

/* ── export row ──────────────────────────────────────────────────── */
.export-row {{
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  margin: 16px 0 32px;
}}
.export-row .small {{ margin-left: auto; }}

/* ── footer ──────────────────────────────────────────────────────── */
footer {{
  display: flex; justify-content: space-between; align-items: center;
  margin-top: 32px; padding-top: 18px;
  border-top: 1px solid {c['border']};
  font-size: 12px;
}}

/* ── toast ───────────────────────────────────────────────────────── */
#toast {{
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: {c['accent']}; color: #001218; padding: 10px 16px;
  border-radius: 999px; font-weight: 600; font-size: 13px;
  opacity: 0; pointer-events: none; transition: opacity 200ms ease;
  z-index: 100;
}}
#toast.show {{ opacity: 1; }}

/* ── demo mode highlight ────────────────────────────────────────── */
body.demo-mode .pill-amber {{
  box-shadow: 0 0 0 1px {c['amber']}, 0 0 18px {c['amber']}55;
}}
body.demo-mode .hero::after {{
  content: ""; position: absolute; inset: 0;
  background: repeating-linear-gradient(
    -45deg, transparent 0 18px, {c['amber']}06 18px 19px
  );
  pointer-events: none;
}}
.hero {{ position: relative; }}

/* ── print stylesheet ────────────────────────────────────────────── */
@media print {{
  @page {{ margin: 14mm; }}
  body {{ background: white; color: #111; }}
  .hero-controls, .export-row, #toast {{ display: none !important; }}
  .card, .panel, .pie-hole {{
    background: white !important; border-color: #ddd !important; color: #111 !important;
  }}
  .kpi::before, .hero::after, body {{ background: white !important; }}
  .label, .muted, .dim, .sub, .bar-x, thead th, .monitor-label, .agent-foot, .small {{
    color: #555 !important;
  }}
  .value, .value-num, .value-md, .pie-total, .mono, .num, .brand-mark, .brand-tag {{
    color: #111 !important; text-shadow: none !important;
  }}
  .pill, .badge {{ color: #111 !important; background: white !important; border-color: #888 !important; }}
  .meter, .agent-bar, .sbar, .cost-bar {{ background: #eee !important; }}
  .meter-bar, .agent-bar-fill, .sbar-fill, .cost-bar > div {{ background: #444 !important; }}
  thead th {{ background: #f5f5f5 !important; color: #111 !important; }}
  tbody td, .call-detail td {{ background: white !important; border-color: #ddd !important; }}
  .pulse {{ display: none; }}
  h1 {{ color: #111 !important; }}
  .panel, .card, .kpi {{ break-inside: avoid; }}
  .charts-grid {{ break-inside: avoid; }}
}}
"""


# =====================================================================
# JavaScript (inlined, no libraries)
# =====================================================================

def _render_js(calls: list) -> str:
    """Inline JS: clock, refresh, sortable table, expand-on-click,
    demo-mode toggle, CSV download, share link, animated KPIs.

    Embedded data lives at window.__cyneaCalls so the CSV button can
    serialize the same rows the table displays.
    """
    embedded = json.dumps(calls)
    return f"""
'use strict';
window.__cyneaCalls = {embedded};

// -- live clock --------------------------------------------------------
function tickClock() {{
  const el = document.getElementById('clock');
  if (!el) return;
  const now = new Date();
  el.textContent = now.toLocaleTimeString();
}}
tickClock();
setInterval(tickClock, 1000);

// -- refresh -----------------------------------------------------------
const refreshBtn = document.getElementById('refreshBtn');
if (refreshBtn) refreshBtn.addEventListener('click', () => location.reload());

// -- demo-mode body toggle --------------------------------------------
const demoBtn = document.getElementById('demoToggle');
if (demoBtn) demoBtn.addEventListener('click', () => {{
  document.body.classList.toggle('demo-mode');
  showToast(document.body.classList.contains('demo-mode')
    ? 'Demo highlight on' : 'Demo highlight off');
}});

// -- expand-on-click rows ---------------------------------------------
document.querySelectorAll('tr.call-row').forEach(row => {{
  row.addEventListener('click', () => {{
    const detail = row.nextElementSibling;
    if (!detail || !detail.classList.contains('call-detail')) return;
    const expanded = row.classList.toggle('expanded');
    detail.hidden = !expanded;
  }});
}});

// -- sortable table ---------------------------------------------------
(function setupSort() {{
  const table = document.getElementById('callsTable');
  if (!table) return;
  const headers = table.querySelectorAll('thead th[data-sort]');
  headers.forEach(h => h.addEventListener('click', () => {{
    const key = h.getAttribute('data-sort');
    const current = h.getAttribute('aria-sort');
    const dir = current === 'ascending' ? 'descending' : 'ascending';
    headers.forEach(o => o.setAttribute('aria-sort', 'none'));
    h.setAttribute('aria-sort', dir);

    const tbody = table.tBodies[0];
    // Pair each call-row with its detail row so we move them together.
    const pairs = [];
    Array.from(tbody.rows).forEach(r => {{
      if (r.classList.contains('call-row')) pairs.push([r, r.nextElementSibling]);
    }});

    const numeric = (v) => {{ const n = parseFloat(v); return isNaN(n) ? -Infinity : n; }};
    const numericKeys = new Set(['time', 'duration', 'turns', 'sentiment', 'cost']);

    pairs.sort((a, b) => {{
      const av = a[0].getAttribute('data-' + key) || '';
      const bv = b[0].getAttribute('data-' + key) || '';
      let cmp;
      if (numericKeys.has(key)) cmp = numeric(av) - numeric(bv);
      else cmp = av.localeCompare(bv);
      return dir === 'ascending' ? cmp : -cmp;
    }});

    pairs.forEach(([row, detail]) => {{
      tbody.appendChild(row);
      if (detail && detail.classList.contains('call-detail')) tbody.appendChild(detail);
    }});
  }}));
}})();

// -- export PDF (just print) ------------------------------------------
const pdfBtn = document.getElementById('exportPdf');
if (pdfBtn) pdfBtn.addEventListener('click', () => window.print());

// -- export CSV -------------------------------------------------------
const csvBtn = document.getElementById('exportCsv');
if (csvBtn) csvBtn.addEventListener('click', () => {{
  const calls = window.__cyneaCalls || [];
  if (!calls.length) {{ showToast('No calls to export'); return; }}
  const headers = [
    'call_id','agent','started_at','duration_s','user_turns','assistant_turns',
    'interruptions','sentiment_score','containment','resolution',
    'handoff_reason','cost_total_cents'
  ];
  const csvLines = [headers.join(',')];
  calls.forEach(c => {{
    const row = headers.map(h => {{
      const v = c[h];
      if (v === null || v === undefined) return '';
      const s = String(v);
      return /[",\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }});
    csvLines.push(row.join(','));
  }});
  const blob = new Blob([csvLines.join('\\n')], {{ type: 'text/csv;charset=utf-8' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'cynea-calls.csv';
  document.body.appendChild(a); a.click();
  setTimeout(() => {{ URL.revokeObjectURL(url); a.remove(); }}, 0);
  showToast('CSV downloaded');
}});

// -- share link -------------------------------------------------------
const shareBtn = document.getElementById('shareLink');
if (shareBtn) shareBtn.addEventListener('click', async () => {{
  const url = location.href;
  try {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      await navigator.clipboard.writeText(url);
      showToast('Link copied to clipboard');
    }} else {{
      window.prompt('Copy this dashboard link:', url);
    }}
  }} catch (e) {{
    window.prompt('Copy this dashboard link:', url);
  }}
}});

// -- toast ------------------------------------------------------------
let toastTimer = null;
function showToast(msg) {{
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}}

// -- KPI count-up animation ------------------------------------------
(function animateKpis() {{
  const cards = document.querySelectorAll('[data-animate-value]');
  cards.forEach(card => {{
    const valEl = card.querySelector('.value');
    if (!valEl) return;
    const final = valEl.getAttribute('data-final') || valEl.textContent;
    const m = final.match(/^(-?[\\d,]+\\.?\\d*)(.*)$/);
    if (!m) return;
    const target = parseFloat(m[1].replace(/,/g, ''));
    if (isNaN(target)) return;
    const suffix = m[2];
    const isInt = !final.includes('.');
    const start = performance.now();
    const dur = 700;
    function step(now) {{
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);
      const v = target * eased;
      valEl.textContent = (isInt ? Math.round(v).toLocaleString() : v.toFixed(1)) + suffix;
      if (t < 1) requestAnimationFrame(step);
      else valEl.textContent = final;  // final exact value
    }}
    requestAnimationFrame(step);
  }});
}})();
"""


# =====================================================================
# CLI
# =====================================================================

# =====================================================================
# Marketing landing page (cynea_landing.html)
# =====================================================================
# Combines the operations dashboard's call-history table with a
# marketing-quality hero, agent showcase, and feature grid. Generated
# as a single self-contained HTML file (only external request is
# Google Fonts).
#
# Public entry point: generate_landing_page(...). Reuses dashboard
# helpers where possible (call history table, status pills, sentiment
# bars) so the two outputs can't drift.

# Marketing copy lives at module scope so the operator can patch it
# without touching the renderer.

LANDING_HEADLINE = "Voice AI Built for Africa"
LANDING_SUBTITLE = (
    "Deploy human-like voice agents for hotels, banks, and call centers — "
    "with African accents, local languages, and zero upfront infrastructure cost."
)

# Showcase metrics for the hero counter strip. Marketing numbers, not
# live data — the operations dashboard at /dashboard.html shows real
# values. Override via the metrics_override argument when you have
# tender-grade real numbers to publish.
LANDING_SHOWCASE_METRICS = {
    "calls_handled": 1247,
    "containment_rate": 0.867,
    "cost_cents": 4.2,
    "active_agents": 2,
}

LANDING_AGENTS = [
    {
        "key": "kwame",
        "name": "Kwame",
        "role": "Hotel Receptionist",
        "country": "Ghana",
        "voice_label": "British male · en-GB-RyanNeural",
        "audio_file": "kwame_test_1.mp3",
        "summary": (
            "Handles bookings, room availability, restaurant hours and small "
            "talk for hospitality clients."
        ),
    },
    {
        "key": "amina",
        "name": "Amina",
        "role": "Customer Service Agent",
        "country": "Kenya",
        "voice_label": "British female · en-GB-SoniaNeural",
        "audio_file": "amina_test_1.mp3",
        "summary": (
            "Banking, telco and e-commerce inquiries with M-Pesa, airtime "
            "and bundle support. Escalates complaints fast."
        ),
    },
]

LANDING_HOW_IT_WORKS = [
    {
        "step": "01",
        "title": "Upload content",
        "body": "Point Cynea at your scripts, PDFs, knowledge base or website. The agent learns your business, not the other way around.",
        "icon": "upload",
    },
    {
        "step": "02",
        "title": "Configure agent",
        "body": "Pick a persona, voice, escalation rules and pricing card. Validate the agent in the dashboard before going live.",
        "icon": "sliders",
    },
    {
        "step": "03",
        "title": "Deploy to phone",
        "body": "Get an Africa's Talking or Twilio number routed to the agent in minutes. Pay per minute used; cancel anytime.",
        "icon": "phone",
    },
]

LANDING_FEATURES = [
    ("African accents & languages",       "English variants for Ghana, Kenya, Nigeria, South Africa — plus mixed Swahili tokens.", "globe"),
    ("Human-like conversations",          "Sequence-id barge-in, grace periods, backchannels. No IVR feel.",                       "speech"),
    ("Free voice processing",             "Local Whisper STT + free Edge TTS by default. You only pay LLM tokens and telephony.",   "bolt"),
    ("Operations dashboard included",     "Per-call sentiment, containment, cost. Export PDF + CSV. Print-friendly.",              "chart"),
    ("Zero infrastructure cost",          "Runs on a single 8 GB box. No Kubernetes, no Redis cluster required.",                  "cloud"),
    ("African phone numbers",             "Africa's Talking integration for Kenya, Nigeria, Ghana, Tanzania, Uganda, Rwanda.",      "phone"),
    ("Sentiment & analytics",             "Per-call sentiment scoring, containment trends, agent-vs-agent comparison.",            "trend"),
    ("24 / 7 availability",               "Engine handles concurrent calls; metrics tracker keeps the audit trail.",                "clock"),
]


def generate_landing_page(
    metrics_file: str = "examples/_out/calls.json",
    output_file: Optional[str] = None,
    *,
    force_demo: bool = False,
    metrics_override: Optional[dict] = None,
) -> str:
    """Render the full marketing + operations landing page.

    Args:
        metrics_file: Path to the calls.json file used to populate the
            call-history table at the bottom.
        output_file: Where to write. Defaults to a sibling
            `cynea_landing.html` next to the input.
        force_demo: Use sample data for the call-history table.
        metrics_override: Override the showcase metrics (the hero
            counter strip) with real numbers. Keys: calls_handled,
            containment_rate, cost_cents, active_agents.

    Returns:
        Absolute path to the file written.
    """
    payload = _load_metrics(metrics_file, force_demo=force_demo)
    calls = list(payload.get("calls") or [])
    is_empty_state = payload.get("_is_empty_state", False)

    showcase = dict(LANDING_SHOWCASE_METRICS)
    if metrics_override:
        showcase.update({k: v for k, v in metrics_override.items() if v is not None})

    if output_file is None:
        base_dir = os.path.dirname(os.path.abspath(metrics_file))
        if not os.path.isdir(base_dir):
            base_dir = os.path.abspath(os.path.join("examples", "_out"))
            os.makedirs(base_dir, exist_ok=True)
        output_file = os.path.join(base_dir, "cynea_landing.html")

    html_text = _render_landing_html(
        calls=calls,
        showcase=showcase,
        is_empty_state=is_empty_state,
    )

    output_file = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(html_text)
    return output_file


# ---------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------

def _render_landing_html(*, calls: list, showcase: dict, is_empty_state: bool) -> str:
    nav     = _landing_render_nav()
    hero    = _landing_render_hero()
    agents  = _landing_render_agent_showcase()
    how     = _landing_render_how_it_works()
    metrics = _landing_render_metrics_bar(showcase)
    feats   = _landing_render_features()
    history = _render_call_history_table(calls, is_empty_state)
    footer  = _landing_render_footer()

    css = _landing_render_css()
    dashboard_css = _render_css()  # for the call-history table styling
    js = _landing_render_js(calls)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cynea Voice Engine — Voice AI built for Africa</title>
<meta name="description" content="{html.escape(LANDING_SUBTITLE)}" />
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{dashboard_css}{css}</style>
</head>
<body class="landing">
{nav}
<main class="landing-main">
  {hero}
  {agents}
  {how}
  {metrics}
  {feats}
  <section id="dashboard" class="dashboard-anchor">
    <div class="section-head">
      <h2>Operations dashboard</h2>
      <p class="muted">Live call history with containment, sentiment and cost per call.</p>
    </div>
    {history}
  </section>
</main>
{footer}
<div id="toast" role="status" aria-live="polite"></div>
<script>{js}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------
# Section renderers (landing-only)
# ---------------------------------------------------------------------

def _landing_render_nav() -> str:
    return """
<nav class="nav" id="topnav">
  <a class="nav-brand" href="#top">
    <span class="brand-mark">CYNEA</span>
    <span class="brand-tag">VOICE ENGINE</span>
  </a>
  <ul class="nav-links">
    <li><a href="#features">Features</a></li>
    <li><a href="#agents">Agents</a></li>
    <li><a href="#dashboard">Dashboard</a></li>
    <li><a href="https://github.com/Mars2390/cynea-voice-engine" target="_blank" rel="noopener">GitHub</a></li>
  </ul>
  <a class="btn btn-outline" id="bookDemo" href="#agents">Book a demo</a>
</nav>
"""


def _landing_render_hero() -> str:
    return f"""
<section class="hero-marketing" id="top">
  <div class="hero-bg" aria-hidden="true"></div>
  <div class="hero-inner">
    <span class="pill pill-cyan">Open source · MIT</span>
    <h1 class="hero-title">{html.escape(LANDING_HEADLINE)}</h1>
    <p class="hero-sub">{html.escape(LANDING_SUBTITLE)}</p>
    <div class="hero-ctas">
      <a class="btn btn-primary" href="#dashboard">See dashboard</a>
      <button class="btn btn-outline play-btn" type="button"
              data-audio="kwame_test_1.mp3"
              data-label="Kwame demo">
        <span class="play-icon" aria-hidden="true">▶</span>
        Hear Kwame demo
      </button>
    </div>
    <div class="hero-trust">
      <span>Designed for hotels, banks, telcos and call centers</span>
      <span class="dim">· Africa's Talking · Twilio · Plivo · SIP</span>
    </div>
  </div>
</section>
"""


def _landing_render_agent_showcase() -> str:
    cards = []
    for agent in LANDING_AGENTS:
        cards.append(f"""
<article class="agent-showcase">
  <header class="agent-showcase-head">
    <div class="agent-name-row">
      <span class="agent-icon" aria-hidden="true">{_landing_icon('user')}</span>
      <h3>{html.escape(agent['name'])}</h3>
      <span class="pill pill-green pill-sm">
        <span class="pulse-dot"></span> Ready for calls
      </span>
    </div>
    <p class="agent-role muted">{html.escape(agent['role'])} · {html.escape(agent['country'])}</p>
  </header>
  <p class="agent-summary">{html.escape(agent['summary'])}</p>
  <dl class="agent-meta">
    <dt>Voice</dt><dd>{html.escape(agent['voice_label'])}</dd>
    <dt>Industry</dt><dd>{html.escape(agent['role'])}</dd>
    <dt>Country</dt><dd>{html.escape(agent['country'])}</dd>
  </dl>
  <div class="agent-actions">
    <button class="btn btn-primary play-btn" type="button"
            data-audio="{html.escape(agent['audio_file'])}"
            data-label="{html.escape(agent['name'])} demo">
      <span class="play-icon" aria-hidden="true">▶</span>
      Hear demo
    </button>
    <a class="btn btn-ghost" href="#dashboard">View metrics</a>
  </div>
</article>
""")
    return f"""
<section class="agents-section" id="agents">
  <div class="section-head">
    <h2>Two agents shipping today</h2>
    <p class="muted">Both run on the same engine. Add a third by writing one Python file plus a JSON config.</p>
  </div>
  <div class="agent-showcase-grid">{"".join(cards)}</div>
</section>
"""


def _landing_render_how_it_works() -> str:
    cards = []
    for s in LANDING_HOW_IT_WORKS:
        cards.append(f"""
<article class="step-card">
  <div class="step-num mono">{html.escape(s['step'])}</div>
  <div class="step-icon">{_landing_icon(s['icon'])}</div>
  <h3>{html.escape(s['title'])}</h3>
  <p class="muted">{html.escape(s['body'])}</p>
</article>
""")
    return f"""
<section class="how-section">
  <div class="section-head">
    <h2>How it works</h2>
    <p class="muted">From quiet repo to live phone number in three steps.</p>
  </div>
  <div class="how-grid">{"".join(cards)}</div>
</section>
"""


def _landing_render_metrics_bar(showcase: dict) -> str:
    """Big counter strip. Counter target lives on data-final; the
    counter animation only fires when the strip scrolls into view."""
    calls_handled = int(showcase.get("calls_handled") or 0)
    containment = float(showcase.get("containment_rate") or 0)
    cost_cents = float(showcase.get("cost_cents") or 0)
    active_agents = int(showcase.get("active_agents") or 0)

    return f"""
<section class="metrics-strip" id="live-metrics">
  <div class="metric">
    <div class="metric-value mono"
         data-counter="{calls_handled}" data-format="int">0</div>
    <div class="metric-label muted">Calls handled</div>
  </div>
  <div class="metric">
    <div class="metric-value mono"
         data-counter="{containment * 100:.1f}" data-format="pct">0%</div>
    <div class="metric-label muted">Containment rate</div>
  </div>
  <div class="metric">
    <div class="metric-value mono"
         data-counter="{cost_cents:.1f}" data-format="cents">0¢</div>
    <div class="metric-label muted">Cost per call</div>
  </div>
  <div class="metric">
    <div class="metric-value mono"
         data-counter="{active_agents}" data-format="int">0</div>
    <div class="metric-label muted">Active agents</div>
  </div>
</section>
"""


def _landing_render_features() -> str:
    cards = []
    for title, body, icon in LANDING_FEATURES:
        cards.append(f"""
<article class="feature-card">
  <div class="feature-icon">{_landing_icon(icon)}</div>
  <h3>{html.escape(title)}</h3>
  <p class="muted">{html.escape(body)}</p>
</article>
""")
    return f"""
<section class="features-section" id="features">
  <div class="section-head">
    <h2>Built for African business</h2>
    <p class="muted">Eight choices we made differently from US-first voice AI platforms.</p>
  </div>
  <div class="features-grid">{"".join(cards)}</div>
</section>
"""


def _landing_render_footer() -> str:
    return f"""
<footer class="landing-footer">
  <div class="footer-inner">
    <div>
      <span class="brand-mark small">CYNEA</span>
      <span class="brand-tag">VOICE ENGINE</span>
    </div>
    <div class="footer-links">
      <a href="https://github.com/Mars2390/cynea-voice-engine" target="_blank" rel="noopener">GitHub</a>
      <a href="#features">Features</a>
      <a href="#agents">Agents</a>
      <a href="#dashboard">Dashboard</a>
    </div>
    <div class="footer-tag dim">
      Cynea AI — Made in Kenya · Built for African business
    </div>
  </div>
</footer>
"""


# ---------------------------------------------------------------------
# Inline SVG icons (Lucide-style stroke icons, ~2 KB total)
# ---------------------------------------------------------------------

def _landing_icon(name: str) -> str:
    icons = {
        "user":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
        "globe":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/></svg>',
        "speech": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
        "bolt":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
        "chart":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="20" x2="21" y2="20"/><rect x="6" y="10" width="3" height="10"/><rect x="11" y="6" width="3" height="14"/><rect x="16" y="13" width="3" height="7"/></svg>',
        "cloud":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19a4.5 4.5 0 1 0-1-8.9A6 6 0 0 0 4.5 12 4 4 0 0 0 5 19h12.5z"/></svg>',
        "phone":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92V21a1 1 0 0 1-1.09 1 19.91 19.91 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.91 19.91 0 0 1 3.21 4.09 1 1 0 0 1 4.2 3h4.09a1 1 0 0 1 1 .76 12.4 12.4 0 0 0 .65 2.62 1 1 0 0 1-.23 1L8 8.91a16 16 0 0 0 6 6l1.51-1.69a1 1 0 0 1 1-.23 12.4 12.4 0 0 0 2.62.65 1 1 0 0 1 .87 1.28z"/></svg>',
        "trend":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
        "clock":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
        "upload": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
        "sliders":'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
    }
    return icons.get(name, '')


# ---------------------------------------------------------------------
# Landing CSS (appended to the dashboard CSS)
# ---------------------------------------------------------------------

def _landing_render_css() -> str:
    c = _COLORS
    return f"""
/* === LANDING PAGE STYLES (additive on top of dashboard CSS) === */

html {{ scroll-behavior: smooth; }}
body.landing {{ overflow-x: hidden; }}
body.landing main.landing-main {{ max-width: 1240px; margin: 0 auto; padding: 0 32px 80px; }}

/* navigation bar */
.nav {{
  position: sticky; top: 0; z-index: 50;
  display: flex; align-items: center; gap: 16px;
  padding: 16px 32px;
  background: {c['bg']}E6;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border-bottom: 1px solid {c['border']};
}}
.nav-brand {{
  display: inline-flex; align-items: baseline; gap: 10px;
  text-decoration: none;
}}
.nav-links {{
  list-style: none; padding: 0; margin: 0 auto;
  display: flex; gap: 28px;
}}
.nav-links a {{
  color: {c['muted']}; text-decoration: none; font-weight: 500;
  font-size: 14px;
  transition: color 120ms ease;
}}
.nav-links a:hover {{ color: {c['text']}; }}
.btn-outline {{
  background: transparent; color: {c['accent']};
  border: 1px solid {c['accent']}; font-weight: 600;
  text-decoration: none; padding: 8px 16px;
  border-radius: 999px;
}}
.btn-outline:hover {{ background: {c['accent']}14; }}
@media (max-width: 720px) {{
  .nav-links {{ display: none; }}
}}

.section-head {{ text-align: center; margin: 0 auto 32px; max-width: 720px; }}
.section-head h2 {{
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 32px; letter-spacing: -0.01em; margin: 0 0 12px;
}}
.section-head p {{ font-size: 15px; }}

/* hero */
.hero-marketing {{
  position: relative;
  padding: 88px 0 96px;
  text-align: center;
  isolation: isolate;
}}
.hero-bg {{
  position: absolute; inset: -10% -10% 0 -10%; z-index: -1;
  background:
    radial-gradient(800px 400px at 30% 30%, {c['accent']}22, transparent 60%),
    radial-gradient(900px 500px at 70% 60%, {c['accent']}14, transparent 65%);
  filter: blur(20px);
  animation: heroDrift 14s ease-in-out infinite alternate;
}}
@keyframes heroDrift {{
  0%   {{ transform: translate3d(-2%, 0, 0) scale(1); }}
  100% {{ transform: translate3d(2%, -2%, 0) scale(1.05); }}
}}
.hero-inner {{ max-width: 820px; margin: 0 auto; }}
.pill-cyan {{ color: {c['accent']}; border-color: {c['accent']}33; background: {c['accent']}14; }}
.pill-sm {{ font-size: 10px; padding: 3px 8px; }}
.hero-title {{
  font-family: 'Syne', sans-serif; font-weight: 800;
  font-size: clamp(40px, 6vw, 64px); line-height: 1.05;
  letter-spacing: -0.02em; margin: 18px 0 16px;
  background: linear-gradient(180deg, {c['text']} 0%, {c['accent']} 220%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.hero-sub {{
  font-size: 17px; color: {c['muted']}; max-width: 640px;
  margin: 0 auto 32px;
}}
.hero-ctas {{
  display: inline-flex; gap: 12px; flex-wrap: wrap; justify-content: center;
  margin-bottom: 32px;
}}
.play-btn {{ display: inline-flex; align-items: center; gap: 8px; }}
.play-icon {{ font-size: 11px; }}
.hero-trust {{ font-size: 12px; color: {c['muted']}; }}

/* agents showcase */
.agents-section {{ padding: 64px 0; }}
.agent-showcase-grid {{
  display: grid; gap: 20px;
  grid-template-columns: repeat(2, 1fr);
}}
@media (max-width: 800px) {{ .agent-showcase-grid {{ grid-template-columns: 1fr; }} }}
.agent-showcase {{
  background: linear-gradient(160deg, {c['card']} 0%, {c['bg3']} 100%);
  border: 1px solid {c['border']}; border-radius: 16px;
  padding: 26px; position: relative; overflow: hidden;
}}
.agent-showcase::before {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, {c['accent']}10, transparent 35%);
}}
.agent-showcase-head {{ margin-bottom: 14px; }}
.agent-name-row {{ display: flex; align-items: center; gap: 12px; }}
.agent-icon {{
  width: 36px; height: 36px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid {c['border_strong']}; border-radius: 10px;
  color: {c['accent']}; background: {c['bg2']};
}}
.agent-icon svg {{ width: 20px; height: 20px; }}
.agent-name-row h3 {{ font-family: 'Syne', sans-serif; font-weight: 700; font-size: 22px; margin: 0; }}
.agent-role {{ margin: 6px 0 0 48px; font-size: 13px; }}
.agent-summary {{ font-size: 14px; margin: 0 0 16px; }}
.agent-meta {{ display: grid; grid-template-columns: 96px 1fr; gap: 6px 14px; margin: 0 0 18px; font-size: 13px; }}
.agent-meta dt {{ color: {c['muted']}; }}
.agent-meta dd {{ margin: 0; font-family: 'JetBrains Mono', monospace; font-size: 12px; }}
.agent-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
.pulse-dot {{
  width: 7px; height: 7px; border-radius: 50%;
  background: {c['green']}; display: inline-block;
  box-shadow: 0 0 0 0 {c['green']}AA;
  animation: pulse 1.6s infinite;
}}

/* how it works */
.how-section {{ padding: 64px 0; }}
.how-grid {{
  display: grid; gap: 16px;
  grid-template-columns: repeat(3, 1fr);
}}
@media (max-width: 900px) {{ .how-grid {{ grid-template-columns: 1fr; }} }}
.step-card {{
  background: {c['card']}; border: 1px solid {c['border']};
  border-radius: 14px; padding: 28px;
  position: relative;
}}
.step-num {{
  font-family: 'JetBrains Mono', monospace; font-weight: 600;
  color: {c['accent']}; font-size: 13px; letter-spacing: 0.1em;
}}
.step-icon {{
  width: 44px; height: 44px; margin: 14px 0 18px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid {c['border_strong']}; border-radius: 12px;
  color: {c['accent']}; background: {c['bg2']};
}}
.step-icon svg {{ width: 22px; height: 22px; }}
.step-card h3 {{ font-family: 'Syne', sans-serif; font-weight: 700; font-size: 18px; margin: 0 0 8px; }}

/* metrics strip */
.metrics-strip {{
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 0;
  margin: 32px 0 64px;
  padding: 28px 0;
  background: {c['card']};
  border: 1px solid {c['border']}; border-radius: 16px;
  position: relative; overflow: hidden;
}}
.metrics-strip::before {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(90deg, {c['accent']}10, transparent 30%, transparent 70%, {c['accent']}10);
}}
@media (max-width: 720px) {{ .metrics-strip {{ grid-template-columns: repeat(2, 1fr); gap: 16px; padding: 16px 0; }} }}
.metric {{
  text-align: center;
  border-right: 1px solid {c['border']};
  padding: 0 16px;
}}
.metric:last-child {{ border-right: none; }}
@media (max-width: 720px) {{ .metric {{ border-right: none; }} }}
.metric-value {{
  font-size: clamp(28px, 4vw, 40px); font-weight: 500;
  color: {c['accent']}; letter-spacing: -0.02em;
  font-feature-settings: "tnum" 1;
}}
.metric-label {{
  font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.08em; margin-top: 8px;
}}

/* features grid */
.features-section {{ padding: 64px 0; }}
.features-grid {{
  display: grid; gap: 16px;
  grid-template-columns: repeat(4, 1fr);
}}
@media (max-width: 1000px) {{ .features-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 540px)  {{ .features-grid {{ grid-template-columns: 1fr; }} }}
.feature-card {{
  background: {c['card']}; border: 1px solid {c['border']};
  border-radius: 14px; padding: 22px;
  transition: border-color 200ms ease, transform 200ms ease;
}}
.feature-card:hover {{ border-color: {c['accent']}55; transform: translateY(-2px); }}
.feature-icon {{
  width: 36px; height: 36px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid {c['border_strong']}; border-radius: 10px;
  color: {c['accent']}; background: {c['bg2']};
  margin-bottom: 12px;
}}
.feature-icon svg {{ width: 18px; height: 18px; }}
.feature-card h3 {{ font-family: 'Syne', sans-serif; font-weight: 700; font-size: 15px; margin: 0 0 8px; }}
.feature-card p {{ font-size: 13px; margin: 0; }}

/* dashboard anchor */
.dashboard-anchor {{ padding: 64px 0 0; scroll-margin-top: 80px; }}

/* footer */
.landing-footer {{
  border-top: 1px solid {c['border']};
  padding: 32px 0;
  margin-top: 32px;
}}
.footer-inner {{
  max-width: 1240px; margin: 0 auto; padding: 0 32px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
}}
.footer-links {{ display: flex; gap: 18px; }}
.footer-links a {{ color: {c['muted']}; text-decoration: none; font-size: 13px; }}
.footer-links a:hover {{ color: {c['text']}; }}
.footer-tag {{ font-size: 12px; }}

/* anchor scroll-margin so sticky nav doesn't cover headings */
#agents, #features, #dashboard {{ scroll-margin-top: 72px; }}
"""


# ---------------------------------------------------------------------
# Landing JS — adds counter-on-scroll + audio play handlers on top of
# the existing dashboard JS (sortable table, expand-on-click, toast).
# ---------------------------------------------------------------------

def _landing_render_js(calls: list) -> str:
    base = _render_js(calls)
    extras = """
// -- audio play buttons (Hear Demo) -----------------------------------
document.querySelectorAll('.play-btn').forEach(btn => {
  btn.addEventListener('click', async (ev) => {
    ev.preventDefault();
    const src = btn.getAttribute('data-audio');
    const label = btn.getAttribute('data-label') || 'Demo';
    if (!src) { showToast('No audio configured for ' + label); return; }
    try {
      const audio = new Audio(src);
      // Probe by attempting to play; the browser will surface the
      // error via the rejected promise if the file 404s.
      await audio.play();
      showToast('Playing ' + label);
    } catch (err) {
      showToast(label + ' audio not generated yet — run examples/hear_kwame.py');
    }
  });
});

// -- counter-on-scroll for the metrics strip --------------------------
(function() {
  const counters = document.querySelectorAll('.metric-value[data-counter]');
  if (!counters.length) return;
  const fired = new WeakSet();

  const animateCounter = (el) => {
    const target = parseFloat(el.getAttribute('data-counter') || '0');
    const fmt = el.getAttribute('data-format') || 'int';
    const start = performance.now();
    const dur = 1200;
    function step(now) {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);
      const v = target * eased;
      if (fmt === 'pct')        el.textContent = v.toFixed(1) + '%';
      else if (fmt === 'cents') el.textContent = v.toFixed(1) + '¢';
      else                      el.textContent = Math.round(v).toLocaleString();
      if (t < 1) requestAnimationFrame(step);
      else {
        if (fmt === 'pct')        el.textContent = target.toFixed(1) + '%';
        else if (fmt === 'cents') el.textContent = target.toFixed(1) + '¢';
        else                      el.textContent = Math.round(target).toLocaleString();
      }
    }
    requestAnimationFrame(step);
  };

  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting && !fired.has(entry.target)) {
          fired.add(entry.target);
          animateCounter(entry.target);
        }
      });
    }, { threshold: 0.4 });
    counters.forEach(el => io.observe(el));
  } else {
    counters.forEach(animateCounter);
  }
})();

// -- nav active link highlighting on scroll ---------------------------
(function() {
  const sections = ['agents', 'features', 'dashboard']
    .map(id => document.getElementById(id)).filter(Boolean);
  const links = document.querySelectorAll('.nav-links a[href^="#"]');
  if (!sections.length || !('IntersectionObserver' in window)) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const id = entry.target.id;
      links.forEach(a => {
        const match = a.getAttribute('href') === '#' + id;
        a.style.color = match ? '#F5F5F5' : '';
      });
    });
  }, { rootMargin: '-30% 0px -65% 0px' });
  sections.forEach(s => io.observe(s));
})();
"""
    return base + "\n" + extras


# =====================================================================
# CLI
# =====================================================================

def _main(argv: list) -> int:
    metrics_file = "examples/_out/calls.json"
    force_demo = False
    landing_mode = False
    args = list(argv[1:])

    if "--demo" in args:
        force_demo = True
        args.remove("--demo")
    if "--landing" in args:
        landing_mode = True
        args.remove("--landing")
    if args:
        metrics_file = args[0]

    try:
        if landing_mode:
            path = generate_landing_page(metrics_file=metrics_file, force_demo=force_demo)
        else:
            path = generate_dashboard(metrics_file=metrics_file, force_demo=force_demo)
    except Exception as exc:
        print(f"[preview] failed to generate output: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
