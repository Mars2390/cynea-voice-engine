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
    cc_bar_html   = _render_callcenter_bar()
    cc_board_html = _render_agent_status_board()
    cc_queue_html = _render_call_queue()
    kpi_html      = _render_kpi_row(summary, calls, is_empty_state)
    monitor_html  = _render_live_monitor(agent_breakdown)
    cc_dept_html  = _render_department_cards()
    cc_roi_html   = _render_roi_calculator()
    cc_heat_html  = _render_call_heatmap()
    table_html    = _render_call_history_table(calls, is_empty_state)
    charts_html   = _render_charts(daily_volume, daily_sentiment, cost_pie, is_empty_state)
    agents_html   = _render_agent_cards(agent_breakdown, is_empty_state)
    cc_feed_html  = _render_call_feed()
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
  {cc_bar_html}
  {cc_board_html}
  {cc_queue_html}
  {kpi_html}
  {monitor_html}
  {cc_dept_html}
  {cc_roi_html}
  {cc_heat_html}
  {charts_html}
  {agents_html}
  {cc_feed_html}
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
# Call-center extension — operations preview
# =====================================================================
# Adds seven sections to the dashboard composition (`_render_html`):
#
#   1. Operations bar     ─ live-feel metric strip (active, today,
#                            waiting, avg wait, local clock)
#   2. Agent status board ─ four agents with live timers and status pills
#   3. Call queue         ─ waiting callers with ticking wait times
#   4. Department cards   ─ per-vertical KPIs with mini sparklines
#   5. ROI calculator     ─ four-agent cost comparison
#   6. Hourly heatmap     ─ 24-bar today-pattern (Ghana business hours)
#   7. Real-time call log ─ scrolling feed with relative timestamps
#
# All numbers are sample data; the page header was deliberately changed
# from "Live Operations" to "Operations Center" so we don't claim live
# ops we aren't yet running. The animated tickers create the *feel* of
# a live operation without making the false claim.
#
# CSS is scoped under .cc-* selectors so nothing leaks into the
# landing page (which inlines the dashboard CSS but doesn't render
# these elements).

# ---------------------------------------------------------------------
# Sample data — patch in place to retell the story for a different
# vertical (e.g. all-banking, all-telco) without touching the renderers.
# ---------------------------------------------------------------------

DASHBOARD_CC_TITLE = "Operations Center"

DASHBOARD_CC_AGENTS = [
    {"key": "kwame", "name": "Kwame", "initials": "K", "dept": "Hotel Desk",
     "accent": "#00D4FF", "status": "on_call",
     "caller": "+233 54 XXX XXXX", "duration_s": 754, "calls_today": 32},
    {"key": "amina", "name": "Amina", "initials": "A", "dept": "Banking",
     "accent": "#A78BFA", "status": "on_call",
     "caller": "+254 72 XXX XXXX", "duration_s": 312, "calls_today": 41},
    {"key": "kofi",  "name": "Kofi",  "initials": "K", "dept": "Support",
     "accent": "#10B981", "status": "available",
     "caller": None, "duration_s": 0, "calls_today": 18},
    {"key": "adwoa", "name": "Adwoa", "initials": "A", "dept": "Complaints",
     "accent": "#F59E0B", "status": "break",
     "caller": None, "duration_s": 0, "calls_today": 9},
]

DASHBOARD_CC_QUEUE = [
    {"position": 1, "caller": "+233 24 XXX XXXX", "wait_s": 47, "dept": "Hotel Desk"},
    {"position": 2, "caller": "+254 71 XXX XXXX", "wait_s": 22, "dept": "Banking"},
    {"position": 3, "caller": "+233 50 XXX XXXX", "wait_s": 8,  "dept": "Support"},
]

DASHBOARD_CC_DEPARTMENTS = [
    {"name": "Hotel Desk", "calls": 124, "containment": 0.94, "sentiment":  0.31, "accent": "#00D4FF",
     "spark": [3, 5, 8, 14, 22, 28, 32, 35, 31, 26, 22, 18]},
    {"name": "Banking",    "calls": 89,  "containment": 0.88, "sentiment":  0.24, "accent": "#A78BFA",
     "spark": [2, 4, 6, 10, 16, 22, 26, 28, 25, 21, 17, 13]},
    {"name": "Support",    "calls": 34,  "containment": 0.76, "sentiment":  0.12, "accent": "#10B981",
     "spark": [1, 2, 3,  5,  7,  9, 11, 12, 11,  9,  7,  6]},
    {"name": "Complaints", "calls": 12,  "containment": 0.45, "sentiment": -0.08, "accent": "#F59E0B",
     "spark": [0, 1, 1,  2,  3,  3,  4,  4,  3,  3,  2,  2]},
]

# Real-time call feed — `ts_offset_s` is seconds before "now" so the
# JS timestamp filler can render relative timestamps that match the
# page-load wall clock.
DASHBOARD_CC_FEED = [
    {"ts_offset_s":   6, "actor": "Kwame",  "msg": "booked deluxe room for Mr. Osei — $120"},
    {"ts_offset_s":  39, "actor": "Amina",  "msg": "resolved balance inquiry for +254 71 XXX XXXX"},
    {"ts_offset_s": 123, "actor": "Kofi",   "msg": "answered FAQ about business hours"},
    {"ts_offset_s": 167, "actor": "Adwoa",  "msg": "escalated complaint to human manager"},
    {"ts_offset_s": 245, "actor": "Kwame",  "msg": "confirmed reservation for Adinkra Hotel"},
    {"ts_offset_s": 318, "actor": "Amina",  "msg": "blocked lost card for +254 72 XXX XXXX"},
    {"ts_offset_s": 390, "actor": "Kofi",   "msg": "explained data bundle options"},
    {"ts_offset_s": 451, "actor": "Kwame",  "msg": "took restaurant booking for 4 guests"},
    {"ts_offset_s": 522, "actor": "Amina",  "msg": "processed M-Pesa reversal — KES 2,000"},
    {"ts_offset_s": 605, "actor": "Adwoa",  "msg": "logged complaint about WiFi outage"},
]

# 24 hourly buckets — Ghana business pattern, peak 10:00-14:00.
DASHBOARD_CC_HEATMAP = [
    2,  1,  1,  0,  0,  1,
    3,  5,  8, 14, 22, 28,
    32, 35, 31, 26, 22, 18,
    14, 11,  8,  5,  3,  2,
]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _format_call_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


_CC_STATUS_LABEL = {"on_call": "On call", "available": "Available", "break": "On break"}
_CC_STATUS_COLOR = {"on_call": "#10B981", "available": "#F59E0B", "break": "#EF4444"}


# ---------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------

def _render_callcenter_bar() -> str:
    return f"""
<section class="cc-bar" id="cc-bar">
  <div class="cc-bar-title">
    <span class="brand-mark small">CYNEA</span>
    <h2>{html.escape(DASHBOARD_CC_TITLE)}</h2>
    <span class="pill pill-green pill-sm">
      <span class="pulse-dot"></span> Online
    </span>
  </div>
  <div class="cc-bar-stats">
    <div class="cc-bar-stat">
      <span class="cc-stat-label">Active agents</span>
      <span class="cc-stat-value mono" data-cc-counter="active_agents">4</span>
    </div>
    <div class="cc-bar-stat">
      <span class="cc-stat-label">Calls today</span>
      <span class="cc-stat-value mono" data-cc-counter="calls_today">247</span>
    </div>
    <div class="cc-bar-stat">
      <span class="cc-stat-label">Waiting</span>
      <span class="cc-stat-value mono" data-cc-counter="waiting">3</span>
    </div>
    <div class="cc-bar-stat">
      <span class="cc-stat-label">Avg wait</span>
      <span class="cc-stat-value mono" data-cc-counter="avg_wait">12s</span>
    </div>
    <div class="cc-bar-stat cc-bar-clock">
      <span class="cc-stat-label">Local time</span>
      <span class="cc-stat-value mono" id="cc-local-clock">--:--:--</span>
    </div>
  </div>
</section>
"""


def _render_agent_status_board() -> str:
    cards = []
    for a in DASHBOARD_CC_AGENTS:
        status = a["status"]
        label = _CC_STATUS_LABEL[status]
        color = _CC_STATUS_COLOR[status]
        timer = (
            f'<div class="cc-agent-timer mono" data-cc-timer="{a["duration_s"]}">'
            f'{_format_call_duration(a["duration_s"])}'
            f'</div>'
        ) if status == "on_call" else (
            f'<div class="cc-agent-timer cc-agent-timer-idle mono">—</div>'
        )
        caller = (
            f'<div class="cc-agent-caller mono small">{html.escape(a["caller"])}</div>'
            if a.get("caller") else
            f'<div class="cc-agent-caller muted small">awaiting next call</div>'
        )
        cards.append(f"""
<article class="cc-agent panel" data-status="{status}" style="--accent:{a['accent']}">
  <header class="cc-agent-head">
    <div class="agent-avatar" style="--accent:{a['accent']}" aria-hidden="true">
      <span>{html.escape(a['initials'])}</span>
    </div>
    <div class="cc-agent-id">
      <h3>{html.escape(a['name'])}</h3>
      <p class="muted small">{html.escape(a['dept'])}</p>
    </div>
    <span class="cc-status-pill"
          style="color:{color};border-color:{color}33;background:{color}14">
      <span class="cc-status-dot" style="background:{color}"></span>{label}
    </span>
  </header>
  {caller}
  {timer}
  <footer class="cc-agent-foot muted small">
    <span>Today</span>
    <span class="mono">{a['calls_today']} calls</span>
  </footer>
</article>
""")
    return f"""
<section class="cc-section" id="cc-agent-board">
  <header class="cc-section-head">
    <h2>Agent status</h2>
    <span class="muted">Four agents · four departments</span>
  </header>
  <div class="cc-agent-grid">{"".join(cards)}</div>
</section>
"""


def _render_call_queue() -> str:
    rows = []
    for q in DASHBOARD_CC_QUEUE:
        rows.append(f"""
<div class="cc-queue-row">
  <span class="cc-queue-pos mono">#{q['position']}</span>
  <span class="cc-queue-caller mono">{html.escape(q['caller'])}</span>
  <span class="cc-queue-dept muted small">{html.escape(q['dept'])}</span>
  <span class="cc-queue-wait mono" data-cc-wait="{q['wait_s']}">{q['wait_s']}s</span>
</div>
""")
    return f"""
<section class="cc-section panel" id="cc-queue">
  <header class="panel-head">
    <h2>Call queue</h2>
    <span class="muted"><span class="mono" data-cc-counter="waiting-mirror">{len(DASHBOARD_CC_QUEUE)}</span> waiting</span>
  </header>
  <div class="cc-queue-list">{"".join(rows)}</div>
</section>
"""


def _render_department_cards() -> str:
    cards = []
    for d in DASHBOARD_CC_DEPARTMENTS:
        s_color = (
            "#10B981" if d["sentiment"] >= 0.2 else
            "#EF4444" if d["sentiment"] <= -0.2 else
            "#8A8A8A"
        )
        c_color = (
            "#10B981" if d["containment"] >= 0.8 else
            "#F59E0B" if d["containment"] >= 0.6 else
            "#EF4444"
        )
        max_v = max(d["spark"]) or 1
        spark = "".join(
            f'<div class="cc-spark-bar" style="height:{(v/max_v)*100:.0f}%; background:{d["accent"]}"></div>'
            for v in d["spark"]
        )
        cards.append(f"""
<article class="cc-dept-card panel" style="--accent:{d['accent']}">
  <header class="cc-dept-head">
    <h3>{html.escape(d['name'])}</h3>
    <span class="cc-dept-badge"
          style="background:{d['accent']}14;color:{d['accent']};border-color:{d['accent']}33">
      {d['calls']} calls
    </span>
  </header>
  <div class="cc-dept-stats">
    <div class="cc-dept-stat">
      <span class="muted small">Containment</span>
      <span class="mono" style="color:{c_color}">{d['containment'] * 100:.0f}%</span>
    </div>
    <div class="cc-dept-stat">
      <span class="muted small">Sentiment</span>
      <span class="mono" style="color:{s_color}">{d['sentiment']:+.2f}</span>
    </div>
  </div>
  <div class="cc-spark" aria-hidden="true">{spark}</div>
</article>
""")
    return f"""
<section class="cc-section" id="cc-departments">
  <header class="cc-section-head">
    <h2>Department performance</h2>
    <span class="muted">Today</span>
  </header>
  <div class="cc-dept-grid">{"".join(cards)}</div>
</section>
"""


def _render_roi_calculator() -> str:
    return """
<section class="cc-section panel cc-roi" id="cc-roi">
  <header class="panel-head">
    <h2>Cost comparison</h2>
    <span class="muted">Monthly · four-agent operation · 10,000 minutes</span>
  </header>
  <div class="cc-roi-body">
    <div class="cc-roi-side cc-roi-human">
      <div class="cc-roi-label muted small">Human agents</div>
      <div class="cc-roi-formula mono">4 agents × $800</div>
      <div class="cc-roi-amount mono">$3,200<span class="muted small"> / month</span></div>
    </div>
    <div class="cc-roi-vs muted">vs</div>
    <div class="cc-roi-side cc-roi-cynea">
      <div class="cc-roi-label small" style="color:#00D4FF">Cynea AI</div>
      <div class="cc-roi-formula mono">4 agents × $0.04 × 10,000 min</div>
      <div class="cc-roi-amount mono" style="color:#00D4FF">$400<span class="muted small"> / month</span></div>
    </div>
  </div>
  <div class="cc-roi-savings">
    <div class="cc-roi-savings-label muted small">Monthly savings</div>
    <div class="cc-roi-savings-num mono">$2,800</div>
    <div class="cc-roi-savings-pct">87.5% reduction · annual savings <span class="mono">$33,600</span></div>
  </div>
</section>
"""


def _render_call_heatmap() -> str:
    max_v = max(DASHBOARD_CC_HEATMAP) or 1
    bars = []
    for hour, value in enumerate(DASHBOARD_CC_HEATMAP):
        height_pct = (value / max_v) * 100
        # Intensity drives opacity for the heat effect.
        intensity = value / max_v
        opacity = 0.35 + 0.65 * intensity
        label = f"{hour:02d}"
        bars.append(f"""
<div class="cc-heat-col" data-hour="{hour}" data-value="{value}" title="{label}:00 — {value} calls">
  <div class="cc-heat-bar" style="height:{height_pct:.0f}%; opacity:{opacity:.2f}"></div>
  <div class="cc-heat-label mono">{label}</div>
</div>
""")
    return f"""
<section class="cc-section panel cc-heatmap" id="cc-heatmap">
  <header class="panel-head">
    <h2>Call volume by hour</h2>
    <span class="muted">Today · 24-hour</span>
  </header>
  <div class="cc-heat-chart">{"".join(bars)}</div>
</section>
"""


def _render_call_feed() -> str:
    rows = []
    for entry in DASHBOARD_CC_FEED:
        rows.append(f"""
<div class="cc-feed-row" data-cc-feed-offset="{entry['ts_offset_s']}">
  <span class="cc-feed-time mono"></span>
  <span class="cc-feed-actor mono">{html.escape(entry['actor'])}</span>
  <span class="cc-feed-msg">{html.escape(entry['msg'])}</span>
</div>
""")
    return f"""
<section class="cc-section panel cc-feed" id="cc-feed">
  <header class="panel-head">
    <h2>Real-time call log</h2>
    <span class="muted">Last hour</span>
  </header>
  <div class="cc-feed-list">{"".join(rows)}</div>
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

/* ── call-center extension ──────────────────────────────────────── */
/* All selectors prefixed `.cc-` so the styles never collide with the
   landing page (which inlines this stylesheet but doesn't render the
   call-center sections).                                             */

.cc-section {{ margin-bottom: 24px; }}
.cc-section-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  padding: 4px 4px 14px; margin: 0 4px;
}}
.cc-section-head h2 {{
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 14px; margin: 0; letter-spacing: 0.06em;
  text-transform: uppercase;
}}

/* operations bar ─────────────────────────────────────────────────── */
.cc-bar {{
  display: flex; align-items: center; justify-content: space-between;
  gap: 24px; flex-wrap: wrap;
  margin: 24px 0;
  padding: 18px 22px;
  background: linear-gradient(160deg, {c['card']} 0%, {c['bg3']} 100%);
  border: 1px solid {c['border']}; border-radius: 14px;
  position: relative; overflow: hidden;
}}
.cc-bar::before {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background-image:
    linear-gradient({c['accent']}0F 1px, transparent 1px),
    linear-gradient(90deg, {c['accent']}0F 1px, transparent 1px);
  background-size: 24px 24px;
  opacity: 0.5;
}}
.cc-bar-title {{
  display: flex; align-items: center; gap: 12px;
  position: relative; z-index: 1;
}}
.cc-bar-title h2 {{
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 18px; margin: 0; letter-spacing: 0.02em;
}}
.cc-bar-stats {{
  display: flex; gap: 28px; flex-wrap: wrap;
  position: relative; z-index: 1;
}}
.cc-bar-stat {{ display: flex; flex-direction: column; gap: 2px; min-width: 86px; }}
.cc-stat-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: {c['muted']}; font-weight: 600;
}}
.cc-stat-value {{
  font-size: 22px; font-weight: 600; color: {c['accent']};
  font-feature-settings: "tnum" 1; letter-spacing: -0.01em;
}}
.cc-bar-clock .cc-stat-value {{ color: {c['text']}; }}
@media (max-width: 720px) {{
  .cc-bar-stats {{ gap: 16px; }}
  .cc-bar-stat {{ min-width: 70px; }}
  .cc-stat-value {{ font-size: 18px; }}
}}

/* agent status board ─────────────────────────────────────────────── */
.cc-agent-grid {{
  display: grid; gap: 16px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}}
@media (max-width: 1000px) {{ .cc-agent-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 540px)  {{ .cc-agent-grid {{ grid-template-columns: 1fr; }} }}
.cc-agent {{
  padding: 18px;
  background: linear-gradient(160deg, {c['card']} 0%, {c['bg3']} 100%);
  border: 1px solid {c['border']}; border-radius: 14px;
  position: relative; overflow: hidden;
}}
.cc-agent::before {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, var(--accent, #00D4FF)0F, transparent 40%);
}}
.cc-agent-head {{
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 12px; position: relative; z-index: 1;
}}
.cc-agent-head .agent-avatar {{ width: 38px; height: 38px; font-size: 16px; }}
.cc-agent-id {{ flex: 1; min-width: 0; }}
.cc-agent-id h3 {{ font-family: 'Syne', sans-serif; font-weight: 700; font-size: 15px; margin: 0; }}
.cc-agent-id p {{ margin: 2px 0 0; }}
.cc-status-pill {{
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px; border-radius: 999px;
  font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; border: 1px solid; line-height: 1;
  white-space: nowrap;
}}
.cc-status-dot {{ width: 6px; height: 6px; border-radius: 50%; display: inline-block; }}
.cc-agent-caller {{ font-size: 12px; margin-bottom: 6px; position: relative; z-index: 1; }}
.cc-agent-timer {{
  font-size: 22px; font-weight: 600; color: {c['accent']};
  font-feature-settings: "tnum" 1; letter-spacing: -0.01em;
  margin-bottom: 12px; position: relative; z-index: 1;
}}
.cc-agent-timer-idle {{ color: {c['dim']}; font-weight: 500; }}
.cc-agent-foot {{
  display: flex; justify-content: space-between; align-items: baseline;
  padding-top: 10px;
  border-top: 1px solid {c['border']};
  position: relative; z-index: 1;
}}

/* call queue ─────────────────────────────────────────────────────── */
.cc-queue-list {{ padding: 10px 22px 18px; }}
.cc-queue-row {{
  display: grid;
  grid-template-columns: 36px 1fr auto auto;
  gap: 14px; align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid {c['border']};
  font-size: 13px;
}}
.cc-queue-row:last-child {{ border-bottom: none; }}
.cc-queue-pos {{ color: {c['accent']}; font-weight: 600; }}
.cc-queue-caller {{ color: {c['text']}; }}
.cc-queue-dept {{ font-size: 11px; }}
.cc-queue-wait {{
  color: {c['amber']}; font-weight: 600;
  min-width: 48px; text-align: right;
}}

/* department cards ───────────────────────────────────────────────── */
.cc-dept-grid {{
  display: grid; gap: 16px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}}
@media (max-width: 1000px) {{ .cc-dept-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 540px)  {{ .cc-dept-grid {{ grid-template-columns: 1fr; }} }}
.cc-dept-card {{
  padding: 18px; border-radius: 14px;
  background: {c['card']}; border: 1px solid {c['border']};
}}
.cc-dept-head {{
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 12px;
}}
.cc-dept-head h3 {{ font-family: 'Syne', sans-serif; font-weight: 700; font-size: 14px; margin: 0; }}
.cc-dept-badge {{
  padding: 3px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 600;
  border: 1px solid;
}}
.cc-dept-stats {{
  display: flex; justify-content: space-between; gap: 16px;
  padding: 8px 0 12px;
}}
.cc-dept-stat {{ display: flex; flex-direction: column; gap: 2px; }}
.cc-dept-stat .mono {{ font-size: 16px; font-weight: 600; }}
.cc-spark {{
  display: flex; align-items: flex-end; gap: 3px;
  height: 32px; padding-top: 4px;
  border-top: 1px solid {c['border']};
}}
.cc-spark-bar {{
  flex: 1; min-height: 3px;
  border-radius: 2px 2px 0 0;
  opacity: 0.85;
}}

/* ROI ─────────────────────────────────────────────────────────────── */
.cc-roi {{ padding: 0; }}
.cc-roi-body {{
  display: grid; grid-template-columns: 1fr auto 1fr;
  gap: 24px; align-items: stretch;
  padding: 22px;
}}
@media (max-width: 720px) {{
  .cc-roi-body {{ grid-template-columns: 1fr; gap: 12px; }}
  .cc-roi-vs {{ display: none; }}
}}
.cc-roi-side {{
  padding: 18px;
  background: {c['bg2']}; border: 1px solid {c['border']};
  border-radius: 12px;
  display: flex; flex-direction: column; gap: 6px;
}}
.cc-roi-cynea {{
  border-color: {c['accent']}33;
  background: linear-gradient(160deg, {c['bg2']} 0%, {c['accent']}0E 100%);
}}
.cc-roi-label {{
  text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600;
}}
.cc-roi-formula {{ font-size: 12px; color: {c['muted']}; }}
.cc-roi-amount {{
  font-size: 32px; font-weight: 600; letter-spacing: -0.02em;
  font-feature-settings: "tnum" 1;
}}
.cc-roi-vs {{
  align-self: center;
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 14px; letter-spacing: 0.16em;
  text-transform: uppercase; color: {c['muted']};
}}
.cc-roi-savings {{
  border-top: 1px solid {c['border']};
  padding: 18px 22px 22px;
  text-align: center;
}}
.cc-roi-savings-label {{
  text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600;
}}
.cc-roi-savings-num {{
  font-size: 56px; font-weight: 700; color: {c['green']};
  letter-spacing: -0.02em; line-height: 1;
  margin: 6px 0 8px;
  font-feature-settings: "tnum" 1;
}}
.cc-roi-savings-pct {{ color: {c['text']}; font-size: 13px; }}

/* heatmap ─────────────────────────────────────────────────────────── */
.cc-heatmap {{ padding: 0; }}
.cc-heat-chart {{
  display: grid; grid-template-columns: repeat(24, 1fr); gap: 4px;
  padding: 18px 22px 22px;
  align-items: end;
  height: 200px;
}}
.cc-heat-col {{
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  height: 100%;
}}
.cc-heat-bar {{
  width: 100%;
  background: linear-gradient(180deg, {c['accent']} 0%, {c['accent_dim']} 100%);
  border-radius: 3px 3px 0 0;
  transition: opacity 200ms ease;
}}
.cc-heat-col:hover .cc-heat-bar {{ opacity: 1 !important; }}
.cc-heat-label {{ font-size: 9px; color: {c['muted']}; }}
@media (max-width: 720px) {{
  .cc-heat-label {{ display: none; }}
  .cc-heat-chart {{ height: 140px; padding: 14px 16px 18px; }}
}}

/* real-time call log ─────────────────────────────────────────────── */
.cc-feed {{ padding: 0; }}
.cc-feed-list {{
  max-height: 320px; overflow-y: auto;
  padding: 8px 22px 18px;
  scrollbar-width: thin; scrollbar-color: {c['border_strong']} transparent;
}}
.cc-feed-list::-webkit-scrollbar {{ width: 6px; }}
.cc-feed-list::-webkit-scrollbar-thumb {{ background: {c['border_strong']}; border-radius: 999px; }}
.cc-feed-row {{
  display: grid; grid-template-columns: 86px 78px 1fr;
  gap: 14px; align-items: baseline;
  padding: 10px 0;
  border-bottom: 1px solid {c['border']};
  font-size: 13px;
  animation: ccFeedIn 320ms ease;
}}
.cc-feed-row:last-child {{ border-bottom: none; }}
.cc-feed-time {{ color: {c['muted']}; font-size: 11px; }}
.cc-feed-actor {{ color: {c['accent']}; font-size: 12px; }}
.cc-feed-msg {{ color: {c['text']}; }}
@keyframes ccFeedIn {{
  from {{ opacity: 0; transform: translateY(-4px); }}
  to   {{ opacity: 1; transform: translateY(0); }}
}}
@media (max-width: 720px) {{
  .cc-feed-row {{ grid-template-columns: 70px 1fr; }}
  .cc-feed-actor {{ grid-column: 1 / -1; }}
}}

/* print ───────────────────────────────────────────────────────────── */
@media print {{
  .cc-bar, .cc-roi-savings-num {{
    background: white !important; color: #111 !important;
  }}
  .cc-heat-bar, .cc-spark-bar {{
    background: #444 !important;
  }}
  .cc-status-pill, .cc-dept-badge {{
    background: white !important; color: #111 !important; border-color: #888 !important;
  }}
  .cc-agent::before, .cc-bar::before {{ display: none !important; }}
  .cc-agent, .cc-dept-card, .cc-feed, .cc-queue, .cc-roi, .cc-heatmap {{
    background: white !important; color: #111 !important; border-color: #ddd !important;
  }}
  .cc-roi-side, .cc-roi-cynea {{ background: #fafafa !important; }}
  .cc-stat-value, .cc-agent-timer, .cc-roi-amount, .cc-roi-savings-num {{ color: #111 !important; }}
  .cc-feed-list {{ max-height: none !important; overflow: visible !important; }}
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
function showToast(msg, duration) {{
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), duration || 1800);
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

// -- call-center extension ---------------------------------------------
// Live tickers for the dashboard's operations sections. Every handler
// is gated on its target element existing, so this code is a no-op on
// the landing page (which inlines this script but doesn't render any
// of the cc-* elements).

(function() {{
  // ── Local clock with seconds (cc-bar). Separate id from the main
  // dashboard clock so neither steps on the other.
  const clockEl = document.getElementById('cc-local-clock');
  if (clockEl) {{
    const tick = () => {{ clockEl.textContent = new Date().toLocaleTimeString(); }};
    tick(); setInterval(tick, 1000);
  }}

  // ── Agent on-call timers — each ticks up every second.
  const timers = document.querySelectorAll('[data-cc-timer]');
  timers.forEach((el) => {{
    let s = parseInt(el.getAttribute('data-cc-timer'), 10) || 0;
    setInterval(() => {{
      s += 1;
      const m = Math.floor(s / 60);
      const sec = s % 60;
      el.textContent = m + 'm ' + String(sec).padStart(2, '0') + 's';
    }}, 1000);
  }});

  // ── Queue wait timers — increment every second so the wait grows
  // realistically while the page is open.
  const waits = document.querySelectorAll('[data-cc-wait]');
  waits.forEach((el) => {{
    let s = parseInt(el.getAttribute('data-cc-wait'), 10) || 0;
    setInterval(() => {{
      s += 1;
      el.textContent = s + 's';
    }}, 1000);
  }});

  // ── Calls-today counter: bump by 1 every ~8 seconds. Subtle enough
  // that a CTO won't notice the regularity, lively enough to feel real.
  const callsTodayEls = document.querySelectorAll('[data-cc-counter="calls_today"]');
  if (callsTodayEls.length) {{
    let v = parseInt((callsTodayEls[0].textContent || '247').replace(/[^0-9]/g, ''), 10) || 247;
    setInterval(() => {{
      v += 1;
      callsTodayEls.forEach((el) => {{ el.textContent = v.toLocaleString(); }});
    }}, 8000);
  }}

  // ── Avg-wait jitter: re-sample every 5 seconds in a believable band.
  const avgWaitEls = document.querySelectorAll('[data-cc-counter="avg_wait"]');
  if (avgWaitEls.length) {{
    setInterval(() => {{
      const v = 10 + Math.floor(Math.random() * 6);  // 10-15 seconds
      avgWaitEls.forEach((el) => {{ el.textContent = v + 's'; }});
    }}, 5000);
  }}

  // ── Real-time feed: render each row's timestamp as `HH:MM:SS` based
  // on `now - data-cc-feed-offset` so the times scroll forward at wall-
  // clock pace. Refresh once a second so the seconds field stays live.
  const feedRows = document.querySelectorAll('[data-cc-feed-offset]');
  if (feedRows.length) {{
    const refresh = () => {{
      const now = Date.now();
      feedRows.forEach((row) => {{
        const offset = parseInt(row.getAttribute('data-cc-feed-offset'), 10) || 0;
        const t = new Date(now - offset * 1000);
        const hh = String(t.getHours()).padStart(2, '0');
        const mm = String(t.getMinutes()).padStart(2, '0');
        const ss = String(t.getSeconds()).padStart(2, '0');
        const slot = row.querySelector('.cc-feed-time');
        if (slot) slot.textContent = hh + ':' + mm + ':' + ss;
      }});
    }};
    refresh();
    setInterval(refresh, 1000);
  }}
}})();
"""


# =====================================================================
# CLI
# =====================================================================

# =====================================================================
# Marketing landing page — cinematic edition
# =====================================================================
# Full-page hero + agent showcase + how-it-works + features + phone
# mockup demo + collapsible call history. Reuses the dashboard's
# call-history table renderer (`_render_call_history_table`) and CSS
# so the two outputs can't drift.
#
# Public entry point: generate_landing_page(...).
# CLI: python -m cynea_africa.dashboard.preview --landing
#
# Audio buttons drive a single shared <audio id="audio-player">. The
# JS function `playDemo(file, fallback, errorMsg)` handles the file ->
# fallback -> toast cascade. Toasts auto-dismiss in 3 s (default for
# the dashboard helper is 1.8 s; the demo path passes 3000 explicitly).

# ------------------------------------------------------------------
# Module-level copy + config — patch these without touching the
# renderer functions below.
# ------------------------------------------------------------------

LANDING_HEADLINE_LEAD = "Voice AI"
LANDING_HEADLINE_ACCENT = "Built for Africa"
LANDING_SUBTITLE = (
    "Deploy human-like voice agents that answer calls, handle "
    "interruptions, and speak with African warmth — in hours, not months."
)

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
        "initials": "K",
        "accent": "#00D4FF",
        "role": "Hotel Receptionist",
        "country": "Ghana",
        "voice_label": "British male · en-GB-RyanNeural",
        # ElevenLabs-synthesized greeting from examples/hear_kwame.py.
        # Fallback to test_2 (the "let me check" line) when the greeting
        # file is missing.
        "audio_file": "kwame_test_1.mp3",
        "audio_fallback": "kwame_test_2.mp3",
        "audio_error": "Kwame demo audio not available — run examples/hear_kwame.py",
        "summary": (
            "Handles bookings, room availability, restaurant hours, and "
            "small talk for hospitality clients."
        ),
    },
    {
        "key": "amina",
        "name": "Amina",
        "initials": "A",
        "accent": "#A78BFA",
        "role": "Customer Service Agent",
        "country": "Kenya",
        "voice_label": "British female · en-GB-SoniaNeural",
        "audio_file": "amina_test_1.mp3",
        "audio_fallback": "",
        "audio_error": "Amina audio not generated yet — run hear_amina.py",
        "summary": (
            "Banking, telco, and e-commerce inquiries with M-Pesa, "
            "airtime, and bundle support. Escalates complaints fast."
        ),
    },
]

LANDING_HOW_STEPS = [
    {"step": "01", "title": "Upload",    "body": "Point Cynea at your scripts, PDFs, knowledge base or website. The agent learns your business, not the other way around.", "icon": "upload"},
    {"step": "02", "title": "Configure", "body": "Pick a persona, voice, language and escalation rules. Validate the agent in the dashboard before going live.",            "icon": "sliders"},
    {"step": "03", "title": "Deploy",    "body": "Get an Africa's Talking or Twilio number routed to the agent in minutes. Pay per minute used; cancel anytime.",            "icon": "phone"},
]

LANDING_FEATURES = [
    ("African Voices",       "Voices for Ghana, Kenya, Nigeria, South Africa — plus mixed Swahili tokens.",            "globe"),
    ("Human-like Speech",    "Sequence-id barge-in, grace periods, backchannels. No IVR feel.",                        "speech"),
    ("Free STT/TTS",         "Local Whisper + free Edge TTS by default. You only pay LLM tokens and telephony.",       "bolt"),
    ("Dashboard Included",   "Per-call sentiment, containment, cost. Export PDF + CSV. Print-friendly.",               "chart"),
    ("Sentiment Analytics",  "Per-call sentiment scoring, containment trends, agent-vs-agent comparison.",             "trend"),
    ("24 / 7 Operation",     "Engine handles concurrent calls; metrics tracker keeps the audit trail.",                "clock"),
]

# Hero floating card + phone-mockup conversation. Speaker is rendered
# in cyan; text is typed character-by-character.
LANDING_HERO_CONVO = [
    {"speaker": "Kwame",  "text": "Hello? Yes, Adinkra Hotel. How can I help you?"},
    {"speaker": "Caller", "text": "I'd like to book a room for Friday."},
    {"speaker": "Kwame",  "text": "Mm, let me check… yes, we have a double available. Two adults?"},
    {"speaker": "Caller", "text": "Yes, two adults. What's the rate?"},
]

LANDING_PHONE_CONVO = [
    {"speaker": "Kwame",  "text": "Hello? Yes, Adinkra Hotel. How can I help?"},
    {"speaker": "Caller", "text": "Are you open this weekend?"},
    {"speaker": "Kwame",  "text": "Yes, all weekend. Friday to Sunday works fine."},
    {"speaker": "Caller", "text": "Perfect. I'd like to book a double."},
    {"speaker": "Kwame",  "text": "Got it. I'll send a confirmation SMS."},
]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def generate_landing_page(
    metrics_file: str = "examples/_out/calls.json",
    output_file: Optional[str] = None,
    *,
    force_demo: bool = False,
    metrics_override: Optional[dict] = None,
) -> str:
    """Render the cinematic marketing landing page.

    Args:
        metrics_file: Path to the calls.json file used to populate the
            collapsible call-history section.
        output_file: Where to write the HTML. Defaults to
            `cynea_landing.html` next to the metrics file.
        force_demo: Use the in-code 15-call sample instead of real data.
        metrics_override: Override the showcase counter strip with real
            numbers (keys: calls_handled, containment_rate, cost_cents,
            active_agents).

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


# ------------------------------------------------------------------
# Top-level renderer
# ------------------------------------------------------------------

def _render_landing_html(*, calls: list, showcase: dict, is_empty_state: bool) -> str:
    loader      = _landing_render_loader()
    nav         = _landing_render_nav()
    hero        = _landing_render_hero()
    metrics     = _landing_render_metric_strip(showcase)
    agents      = _landing_render_agent_showcase()
    chat_widget = _landing_render_chat_widget()
    timeline    = _landing_render_timeline()
    feats       = _landing_render_features()
    phone       = _landing_render_phone_demo()
    table       = _render_call_history_table(calls, is_empty_state)
    footer      = _landing_render_footer()

    css = _landing_render_css()
    dashboard_css = _render_css()
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
<div id="cynea-scroll-progress" aria-hidden="true"></div>
<div id="cynea-cursor-glow" aria-hidden="true"></div>
{loader}
{nav}
{metrics}
<main class="landing-main">
  {hero}
  {agents}
  {chat_widget}
  {timeline}
  {feats}
  {phone}
  <section id="dashboard" class="dashboard-anchor">
    <div class="section-head">
      <span class="section-eyebrow">Operations</span>
      <h2>Operations dashboard</h2>
      <p class="muted">Call history with containment, sentiment, and cost per call — exportable on demand.</p>
    </div>
    <details class="data-collapse" open>
      <summary>
        <span class="data-collapse-label">See live data</span>
        <span class="data-collapse-icon" aria-hidden="true">▾</span>
      </summary>
      <div class="data-collapse-body">{table}</div>
    </details>
  </section>
</main>
{footer}
<button id="cynea-back-to-top" type="button" aria-label="Back to top" hidden>
  <span aria-hidden="true">↑</span>
</button>
<div id="toast" role="status" aria-live="polite"></div>
<audio id="audio-player" preload="none" aria-label="Demo audio player"></audio>
<script>{js}</script>
</body>
</html>
"""


# ------------------------------------------------------------------
# Section renderers
# ------------------------------------------------------------------

def _landing_render_loader() -> str:
    """Cinematic loader: black screen, then C-Y-N-E-A appears letter
    by letter in cyan with a glow, pulses once, fades out."""
    letters = "".join(f'<span aria-hidden="true">{c}</span>' for c in "CYNEA")
    return f"""
<div class="cynea-loader" id="cyneaLoader" aria-hidden="true">
  <div class="cynea-loader-inner">
    <div class="cynea-loader-text">{letters}</div>
    <div class="cynea-loader-tag">VOICE ENGINE</div>
  </div>
</div>
"""


def _landing_render_nav() -> str:
    return """
<nav class="nav landing-nav" id="topnav">
  <a class="nav-brand" href="#top">
    <span class="brand-mark">CYNEA</span>
    <span class="brand-tag">VOICE ENGINE</span>
  </a>
  <ul class="nav-links">
    <li><a href="#agents">Agents</a></li>
    <li><a href="#timeline">How it works</a></li>
    <li><a href="#features">Features</a></li>
    <li><a href="#dashboard">Dashboard</a></li>
    <li><a href="https://github.com/Mars2390/cynea-voice-engine" target="_blank" rel="noopener">GitHub</a></li>
  </ul>
  <a class="nav-signin" href="signin.html">Sign in</a>
  <a class="btn btn-outline" id="bookDemo" href="agent_manager.html">Get started</a>
</nav>
"""


def _landing_render_hero() -> str:
    convo_lines = "".join(
        f'<div class="convo-line" data-speaker="{html.escape(c["speaker"])}" '
        f'data-text="{html.escape(c["text"])}"></div>'
        for c in LANDING_HERO_CONVO
    )
    return f"""
<section class="hero-cinematic" id="top">
  <div class="hero-bg" aria-hidden="true">
    <div class="hero-grid-bg"></div>
    <div class="hero-orb hero-orb-1"></div>
    <div class="hero-orb hero-orb-2"></div>
    <div class="hero-scanline"></div>
  </div>
  <div class="hero-grid">
    <div class="hero-text">
      <h1 class="hero-title">
        <span class="hero-line-1">{html.escape(LANDING_HEADLINE_LEAD)}</span>
        <span class="hero-line-2">{html.escape(LANDING_HEADLINE_ACCENT)}</span>
      </h1>
      <p class="hero-sub">{html.escape(LANDING_SUBTITLE)}</p>
      <div class="hero-ctas">
        <a class="btn btn-primary btn-glow" href="agent_manager.html">
          See dashboard <span aria-hidden="true">→</span>
        </a>
        <button class="btn btn-outline btn-glow play-btn" type="button"
                data-audio="kwame_test_1.mp3"
                data-audio-fallback="kwame_test_2.mp3"
                data-error="Kwame demo audio not available — run examples/hear_kwame.py"
                data-label="Kwame demo">
          <span class="play-icon" aria-hidden="true">▶</span>
          Hear Kwame demo
        </button>
      </div>
      <div class="hero-trust">
        <span>Hospitality · Banking · Telco · Government</span>
        <span class="dim">· Africa's Talking · Twilio · Plivo · SIP</span>
      </div>
    </div>
    <div class="hero-card-wrap">
      <div class="hero-card" data-tilt id="heroDemo">
        <div class="hero-card-glare" aria-hidden="true"></div>
        <div class="hero-card-inner">
          <header class="hero-card-head">
            <span class="hero-card-title">Live call · Adinkra Hotel</span>
            <span class="pill pill-green pill-sm">
              <span class="pulse-dot"></span> Connected
            </span>
          </header>
          <div class="convo" data-typewriter>
            {convo_lines}
          </div>
          <footer class="hero-card-foot mono small">
            <span>00:18</span><span class="dim"> · 8 kHz μ-law</span>
          </footer>
        </div>
      </div>
    </div>
  </div>
</section>
"""


def _landing_render_metric_strip(showcase: dict) -> str:
    """Sticky strip directly under the nav. Each metric animates when
    it scrolls into view (count-up, circular progress, sparkline grow,
    pulse on)."""
    calls_handled = int(showcase.get("calls_handled") or 0)
    containment = float(showcase.get("containment_rate") or 0)
    cost_cents = float(showcase.get("cost_cents") or 0)
    active_agents = int(showcase.get("active_agents") or 0)

    # Circular progress — 26-radius circle, circumference ~163.36
    circumference = 163.36
    dash_offset = circumference * (1.0 - max(0.0, min(1.0, containment)))

    return f"""
<section class="metric-strip" id="metric-strip" aria-label="Cynea Voice Engine showcase metrics">
  <div class="metric-strip-inner">
    <div class="metric-cell">
      <div class="metric-num mono"
           data-counter="{calls_handled}" data-format="int">0</div>
      <div class="metric-label muted">Calls handled</div>
    </div>
    <div class="metric-cell metric-circle-cell">
      <div class="metric-circle">
        <svg viewBox="0 0 60 60" aria-hidden="true">
          <circle cx="30" cy="30" r="26" fill="none" stroke="#1E1E1E" stroke-width="4"/>
          <circle cx="30" cy="30" r="26" fill="none" stroke="#00D4FF" stroke-width="4"
                  stroke-linecap="round"
                  stroke-dasharray="{circumference:.2f}"
                  stroke-dashoffset="{circumference:.2f}"
                  data-progress-target="{dash_offset:.2f}"
                  transform="rotate(-90 30 30)" />
        </svg>
        <span class="metric-circle-text mono"
              data-counter="{containment * 100:.1f}" data-format="pct">0%</span>
      </div>
      <div class="metric-label muted">Containment</div>
    </div>
    <div class="metric-cell">
      <div class="metric-num mono"
           data-counter="{cost_cents:.1f}" data-format="cents">0¢</div>
      <svg class="metric-sparkline" viewBox="0 0 80 24" preserveAspectRatio="none" aria-hidden="true">
        <polyline points="0,20 12,18 24,16 36,14 48,11 60,9 72,7 80,6"
                  fill="none" stroke="#00D4FF" stroke-width="1.5"
                  stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div class="metric-label muted">Cost / call</div>
    </div>
    <div class="metric-cell">
      <div class="metric-num mono">
        <span class="pulse-dot pulse-dot-large"></span>
        <span data-counter="{active_agents}" data-format="int">0</span>
      </div>
      <div class="metric-label muted">Active agents</div>
    </div>
  </div>
</section>
"""


def _landing_render_agent_showcase() -> str:
    cards = []
    for agent in LANDING_AGENTS:
        accent = agent["accent"]
        cards.append(f"""
<article class="agent-card-cinematic reveal" data-tilt
         style="--accent:{accent};--accent-soft:{accent}33;--accent-glow:{accent}55">
  <div class="agent-card-glare" aria-hidden="true"></div>
  <div class="agent-card-inner">
    <header class="agent-card-head">
      <div class="agent-avatar" aria-hidden="true"
           style="--accent:{accent}">
        <span>{html.escape(agent['initials'])}</span>
      </div>
      <div class="agent-id">
        <h3>{html.escape(agent['name'])}</h3>
        <p class="muted">{html.escape(agent['role'])} · {html.escape(agent['country'])}</p>
      </div>
      <span class="pill pill-green pill-sm pulse-pill">
        <span class="pulse-dot"></span> Ready for calls
      </span>
    </header>
    <p class="agent-summary">{html.escape(agent['summary'])}</p>
    <dl class="agent-meta">
      <dt>Voice</dt><dd>{html.escape(agent['voice_label'])}</dd>
      <dt>Industry</dt><dd>{html.escape(agent['role'])}</dd>
      <dt>Country</dt><dd>{html.escape(agent['country'])}</dd>
    </dl>
    <div class="agent-actions">
      <button class="btn btn-primary btn-glow play-btn" type="button"
              data-audio="{html.escape(agent['audio_file'])}"
              data-audio-fallback="{html.escape(agent.get('audio_fallback') or '')}"
              data-error="{html.escape(agent.get('audio_error') or 'Demo audio not available')}"
              data-label="{html.escape(agent['name'])} demo">
        <span class="play-icon" aria-hidden="true">▶</span>
        Hear demo
      </button>
      <a class="btn btn-ghost" href="#dashboard">View metrics</a>
    </div>
  </div>
</article>
""")
    return f"""
<section class="agents-section" id="agents">
  <div class="section-head">
    <span class="section-eyebrow">Agents</span>
    <h2>Two agents shipping today</h2>
    <p class="muted">Both run on the same engine. Add a third by writing one Python file plus a JSON config.</p>
  </div>
  <div class="agent-showcase-grid">{"".join(cards)}</div>
</section>
"""


def _landing_render_chat_widget() -> str:
    """Interactive chat widget: visitors type a message; Kwame replies
    via a keyword-routed response map (no backend, no LLM call). The
    JS layer drives the typewriter, the chip suggestions, and audio
    playback through the shared <audio id="audio-player"> element.
    """
    # Suggested questions surfaced as chips at the bottom of the chat
    # area. Clicking a chip auto-sends its `data-message`.
    chips = [
        "I'd like to book a room",
        "What are your rates?",
        "Do you have a pool?",
        "I have a complaint",
    ]
    chips_html = "".join(
        f'<button type="button" class="chat-chip" '
        f'data-message="{html.escape(msg)}">{html.escape(msg)}</button>'
        for msg in chips
    )
    return f"""
<section class="chat-demo-section reveal" id="chat-demo">
  <div class="section-head">
    <span class="section-eyebrow">Try it now</span>
    <h2>Talk to Kwame, live</h2>
    <p class="muted">No sign-up. Type a question or tap a suggestion. Kwame replies with the same engine that powers production.</p>
  </div>
  <div class="chat-demo">
    <header class="chat-header">
      <span class="pulse-dot" aria-hidden="true"></span>
      <div class="chat-header-id">
        <div class="chat-header-title">Adinkra Hotel, Accra</div>
        <div class="chat-header-status muted">Kwame · Online · AI assistant</div>
      </div>
    </header>
    <div class="chat-messages" id="chat-messages" role="log" aria-live="polite"></div>
    <div class="chat-chips" id="chat-chips" aria-label="Suggested questions">{chips_html}</div>
    <form class="chat-input-row" id="chat-form" autocomplete="off">
      <input class="chat-input" id="chat-input" type="text" inputmode="text"
             placeholder="Ask Kwame anything…" aria-label="Type a message"
             maxlength="200">
      <button class="chat-send" id="chat-send" type="submit" aria-label="Send" title="Send">
        <span aria-hidden="true">→</span>
      </button>
    </form>
  </div>
</section>
"""


def _landing_render_timeline() -> str:
    cards = []
    for i, s in enumerate(LANDING_HOW_STEPS):
        cards.append(f"""
<article class="timeline-step reveal" style="--reveal-delay:{i * 120}ms">
  <div class="timeline-dot" aria-hidden="true"></div>
  <div class="timeline-num mono">{html.escape(s['step'])}</div>
  <div class="timeline-icon">{_landing_icon(s['icon'])}</div>
  <h3>{html.escape(s['title'])}</h3>
  <p class="muted">{html.escape(s['body'])}</p>
</article>
""")
    return f"""
<section class="timeline-section" id="timeline">
  <div class="section-head">
    <span class="section-eyebrow">How it works</span>
    <h2>From quiet repo to live phone number</h2>
    <p class="muted">Three steps. Same engine, any client.</p>
  </div>
  <div class="timeline">
    <div class="timeline-rail" aria-hidden="true"></div>
    {"".join(cards)}
  </div>
</section>
"""


def _landing_render_features() -> str:
    cards = []
    for i, (title, body, icon) in enumerate(LANDING_FEATURES):
        cards.append(f"""
<article class="feature-card-cinematic reveal" style="--reveal-delay:{i * 80}ms">
  <div class="feature-card-glow" aria-hidden="true"></div>
  <div class="feature-icon">{_landing_icon(icon)}</div>
  <h3>{html.escape(title)}</h3>
  <p class="muted">{html.escape(body)}</p>
</article>
""")
    return f"""
<section class="features-section" id="features">
  <div class="section-head">
    <span class="section-eyebrow">Features</span>
    <h2>Eight choices we made differently</h2>
    <p class="muted">Where US-first voice AI platforms get Africa wrong, and where we get it right.</p>
  </div>
  <div class="features-grid">{"".join(cards)}</div>
</section>
"""


def _landing_render_phone_demo() -> str:
    convo_lines = "".join(
        f'<div class="phone-line phone-line-{("agent" if c["speaker"]=="Kwame" else "caller")}" '
        f'data-speaker="{html.escape(c["speaker"])}" '
        f'data-text="{html.escape(c["text"])}"></div>'
        for c in LANDING_PHONE_CONVO
    )
    return f"""
<section class="phone-demo reveal" id="phone-demo">
  <div class="section-head">
    <span class="section-eyebrow">Try it</span>
    <h2>This is what your customer hears</h2>
    <p class="muted">Click play. The audio is generated by the same engine that powers the dashboard above.</p>
  </div>
  <div class="phone-stage">
    <div class="phone">
      <div class="phone-frame">
        <div class="phone-notch" aria-hidden="true"></div>
        <div class="phone-screen">
          <div class="phone-status mono small">CYNEA · 16:42 · 4G</div>
          <div class="phone-callee">
            <div class="agent-avatar" style="--accent:#00D4FF" aria-hidden="true"><span>K</span></div>
            <div class="phone-callee-id">
              <div class="phone-name">Kwame</div>
              <div class="phone-role muted">Adinkra Hotel · 00:18</div>
            </div>
            <span class="pill pill-green pill-sm">
              <span class="pulse-dot"></span> Live
            </span>
          </div>
          <div class="phone-convo" data-typewriter>{convo_lines}</div>
          <div class="phone-controls">
            <button class="phone-play play-btn" type="button"
                    data-audio="kwame_test_1.mp3"
                    data-audio-fallback="kwame_test_2.mp3"
                    data-error="Audio not available offline — run examples/hear_kwame.py"
                    data-label="Kwame call audio"
                    aria-label="Play call audio">
              <span class="phone-play-icon" data-state="play" aria-hidden="true">▶</span>
              <span class="phone-play-label">Play actual call audio</span>
            </button>
          </div>
        </div>
      </div>
    </div>
    <aside class="phone-sidecar">
      <h3>Engineered for African telephony</h3>
      <p class="muted">Production-ready integrations with Africa's Talking, Twilio, Plivo and SIP. Route inbound calls to your agents in minutes, not weeks.</p>
      <div class="phone-cta-row">
        <a class="btn btn-primary btn-glow" href="#agents">Book a demo</a>
        <a class="btn btn-ghost" href="https://github.com/Mars2390/cynea-voice-engine" target="_blank" rel="noopener">View on GitHub</a>
      </div>
      <div class="phone-feature-row">
        <div class="phone-feature">
          <strong class="mono">800ms</strong>
          <span class="muted small">P95 response latency</span>
        </div>
        <div class="phone-feature">
          <strong class="mono">87%</strong>
          <span class="muted small">Containment rate</span>
        </div>
        <div class="phone-feature">
          <strong class="mono">4.2¢</strong>
          <span class="muted small">Avg cost / call</span>
        </div>
      </div>
    </aside>
  </div>
</section>
"""


def _landing_render_footer() -> str:
    return """
<footer class="landing-footer">
  <div class="footer-inner">
    <div class="footer-brand">
      <span class="brand-mark large">CYNEA</span>
      <span class="brand-tag">VOICE ENGINE</span>
    </div>
    <div class="footer-cols">
      <div class="footer-col">
        <h4>Engine</h4>
        <a href="#agents">Agents</a>
        <a href="#features">Features</a>
        <a href="#dashboard">Dashboard</a>
      </div>
      <div class="footer-col">
        <h4>Project</h4>
        <a href="https://github.com/Mars2390/cynea-voice-engine" target="_blank" rel="noopener">GitHub</a>
        <a href="https://github.com/Mars2390/cynea-voice-engine/blob/main/README.md" target="_blank" rel="noopener">README</a>
        <a href="https://github.com/Mars2390/cynea-voice-engine/issues" target="_blank" rel="noopener">Issues</a>
      </div>
      <div class="footer-col">
        <h4>Cynea AI</h4>
        <span class="muted small">Made in Kenya</span>
        <span class="muted small">Built for African business</span>
        <span class="dim small">© 2026 Cynea AI</span>
      </div>
    </div>
  </div>
</footer>
"""


# ------------------------------------------------------------------
# Inline SVG icons (Lucide-style)
# ------------------------------------------------------------------

def _landing_icon(name: str) -> str:
    icons = {
        "globe":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/></svg>',
        "speech": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
        "bolt":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
        "chart":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="20" x2="21" y2="20"/><rect x="6" y="10" width="3" height="10"/><rect x="11" y="6" width="3" height="14"/><rect x="16" y="13" width="3" height="7"/></svg>',
        "trend":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
        "clock":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
        "upload": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
        "sliders":'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
        "phone":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92V21a1 1 0 0 1-1.09 1 19.91 19.91 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.91 19.91 0 0 1 3.21 4.09 1 1 0 0 1 4.2 3h4.09a1 1 0 0 1 1 .76 12.4 12.4 0 0 0 .65 2.62 1 1 0 0 1-.23 1L8 8.91a16 16 0 0 0 6 6l1.51-1.69a1 1 0 0 1 1-.23 12.4 12.4 0 0 0 2.62.65 1 1 0 0 1 .87 1.28z"/></svg>',
    }
    return icons.get(name, '')


# ------------------------------------------------------------------
# CSS — landing-only styles, layered over dashboard CSS
# ------------------------------------------------------------------

def _landing_render_css() -> str:
    """Cinematic landing CSS. Plain triple-quoted string — no f-string
    so { } in selectors don't need escaping. All color tokens are
    hardcoded to keep the file readable."""
    return r"""
/* === LANDING (cinematic) — appended on top of dashboard CSS === */

html { scroll-behavior: smooth; }
body.landing { overflow-x: hidden; background: #050505; }
body.landing main.landing-main { max-width: 1240px; margin: 0 auto; padding: 0 32px 80px; }

/* Respect users who asked to slow down. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}

/* ── loader overlay ─────────────────────────────────────────────── */
.cynea-loader {
  position: fixed; inset: 0; z-index: 9999;
  background: #050505;
  display: flex; align-items: center; justify-content: center;
  animation: loaderFade 0.5s ease 1.9s forwards;
  pointer-events: auto;
}
.cynea-loader-inner { text-align: center; }
.cynea-loader-text {
  font-family: 'Syne', sans-serif; font-weight: 800;
  font-size: clamp(56px, 12vw, 120px);
  letter-spacing: 0.18em; color: #00D4FF;
  display: inline-flex; gap: 0.04em;
  filter: drop-shadow(0 0 24px rgba(0,212,255,0.5));
}
.cynea-loader-text span {
  display: inline-block;
  opacity: 0; transform: translateY(8px); filter: blur(6px);
  animation: letterIn 0.5s ease forwards, letterPulse 0.7s ease 1.0s 1;
}
.cynea-loader-text span:nth-child(1) { animation-delay: 0.5s, 1.0s; }
.cynea-loader-text span:nth-child(2) { animation-delay: 0.6s, 1.0s; }
.cynea-loader-text span:nth-child(3) { animation-delay: 0.7s, 1.0s; }
.cynea-loader-text span:nth-child(4) { animation-delay: 0.8s, 1.0s; }
.cynea-loader-text span:nth-child(5) { animation-delay: 0.9s, 1.0s; }
.cynea-loader-tag {
  font-family: 'Syne', sans-serif; font-weight: 600;
  font-size: 12px; letter-spacing: 0.32em; color: #5F5F5F;
  margin-top: 18px;
  opacity: 0;
  animation: letterIn 0.6s ease 1.4s forwards;
}
@keyframes letterIn {
  0%   { opacity: 0; transform: translateY(8px); filter: blur(6px); }
  100% { opacity: 1; transform: translateY(0); filter: blur(0); }
}
@keyframes letterPulse {
  0%   { transform: scale(1); text-shadow: 0 0 12px rgba(0,212,255,0.6); }
  50%  { transform: scale(1.06); text-shadow: 0 0 36px rgba(0,212,255,0.9), 0 0 60px rgba(0,212,255,0.4); }
  100% { transform: scale(1); text-shadow: 0 0 12px rgba(0,212,255,0.6); }
}
@keyframes loaderFade {
  to { opacity: 0; pointer-events: none; visibility: hidden; }
}
.cynea-loader.done { display: none !important; }

/* Body fades in once the loader has played. */
body.landing main.landing-main,
body.landing > nav.landing-nav,
body.landing > .metric-strip,
body.landing > footer.landing-footer {
  animation: contentFadeIn 0.6s ease 1.6s both;
}
@keyframes contentFadeIn { from { opacity: 0; } to { opacity: 1; } }

/* ── nav ────────────────────────────────────────────────────────── */
.landing-nav {
  position: sticky; top: 0; z-index: 50;
  display: flex; align-items: center; gap: 16px;
  padding: 14px 32px;
  background: rgba(5,5,5,0.85);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid #1E1E1E;
}
.nav-brand { display: inline-flex; align-items: baseline; gap: 10px; text-decoration: none; }
.nav-links {
  list-style: none; padding: 0; margin: 0 auto;
  display: flex; gap: 28px;
}
.nav-links a {
  color: #8A8A8A; text-decoration: none; font-weight: 500; font-size: 14px;
  transition: color 120ms ease;
}
.nav-links a:hover { color: #F5F5F5; }
.btn-outline {
  background: transparent; color: #00D4FF;
  border: 1px solid #00D4FF; font-weight: 600;
  text-decoration: none; padding: 8px 16px; border-radius: 999px;
}
.btn-outline:hover { background: rgba(0,212,255,0.08); }
.nav-signin {
  color: #F5F5F5; text-decoration: none; font-weight: 500;
  font-size: 14px; padding: 6px 4px;
  margin-right: 4px;
  position: relative;
}
.nav-signin::after {
  content: ""; position: absolute; left: 0; right: 0; bottom: -2px;
  height: 1px; background: #00D4FF;
  transform: scaleX(0); transform-origin: left center;
  transition: transform 240ms cubic-bezier(.2,.8,.2,1);
}
.nav-signin:hover::after { transform: scaleX(1); }
@media (max-width: 540px) { .nav-signin { display: none; } }
.btn-glow { transition: box-shadow 200ms ease, transform 120ms ease, background 200ms ease; }
.btn-primary.btn-glow:hover { box-shadow: 0 0 0 1px #00D4FF, 0 0 30px rgba(0,212,255,0.45); }
.btn-outline.btn-glow:hover { box-shadow: 0 0 0 1px #00D4FF, 0 0 24px rgba(0,212,255,0.25); }
@media (max-width: 720px) { .nav-links { display: none; } }

/* ── section heads ──────────────────────────────────────────────── */
.section-head { text-align: center; margin: 0 auto 36px; max-width: 720px; }
.section-eyebrow {
  display: inline-block; font-family: 'JetBrains Mono', monospace; font-weight: 600;
  font-size: 11px; letter-spacing: 0.18em; color: #00D4FF;
  text-transform: uppercase; margin-bottom: 10px;
}
.section-head h2 {
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 36px; letter-spacing: -0.01em; margin: 0 0 12px;
}
.section-head p { font-size: 15px; }

/* ── hero ───────────────────────────────────────────────────────── */
.hero-cinematic {
  position: relative; padding: 80px 0 96px; isolation: isolate;
}
.hero-bg { position: absolute; inset: -10% -10% 0 -10%; z-index: -1; overflow: hidden; }
.hero-grid-bg {
  position: absolute; inset: 0;
  background-image:
    linear-gradient(rgba(0,212,255,0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,255,0.05) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(ellipse 700px 500px at 30% 30%, black 30%, transparent 80%);
  -webkit-mask-image: radial-gradient(ellipse 700px 500px at 30% 30%, black 30%, transparent 80%);
  opacity: 0.7;
}
.hero-orb {
  position: absolute; border-radius: 50%; filter: blur(60px);
  pointer-events: none;
}
.hero-orb-1 {
  width: 520px; height: 520px;
  left: -8%; top: 10%;
  background: radial-gradient(circle, rgba(0,212,255,0.28), transparent 60%);
  animation: orbDrift 14s ease-in-out infinite alternate;
}
.hero-orb-2 {
  width: 460px; height: 460px;
  right: -6%; top: 30%;
  background: radial-gradient(circle, rgba(167,139,250,0.18), transparent 60%);
  animation: orbDrift 18s ease-in-out infinite alternate-reverse;
}
@keyframes orbDrift {
  from { transform: translate3d(-2%, 0, 0) scale(1); }
  to   { transform: translate3d(2%, -2%, 0) scale(1.06); }
}
.hero-scanline {
  position: absolute; left: 0; right: 0; top: 0; height: 2px;
  background: linear-gradient(90deg, transparent, rgba(0,212,255,0.5), transparent);
  filter: blur(1px);
  animation: scanline 6s linear infinite;
}
@keyframes scanline {
  0%   { transform: translateY(0); opacity: 0; }
  10%  { opacity: 1; }
  100% { transform: translateY(640px); opacity: 0; }
}

.hero-grid {
  display: grid; grid-template-columns: 1.1fr 1fr; gap: 48px;
  align-items: center;
}
@media (max-width: 1000px) { .hero-grid { grid-template-columns: 1fr; gap: 32px; } }

.hero-text { max-width: 600px; }
.hero-title {
  font-family: 'Syne', sans-serif; font-weight: 800;
  font-size: clamp(40px, 6vw, 68px); line-height: 1.02;
  letter-spacing: -0.02em; margin: 18px 0 18px;
}
.hero-line-1 { display: block; color: #F5F5F5; }
.hero-line-2 {
  display: block;
  background: linear-gradient(110deg, #00D4FF 0%, #A78BFA 60%, #00D4FF 110%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
  background-size: 200% auto;
  animation: gradientShift 8s ease-in-out infinite;
}
@keyframes gradientShift {
  0%   { background-position: 0% 50%; }
  50%  { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}
.hero-sub { font-size: 17px; color: #8A8A8A; max-width: 540px; margin: 0 0 28px; }
.hero-ctas { display: inline-flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }
.play-btn { display: inline-flex; align-items: center; gap: 8px; }
.play-icon { font-size: 11px; }
.hero-trust { font-size: 12px; color: #8A8A8A; }

/* hero floating card */
.hero-card-wrap { perspective: 1200px; }
.hero-card {
  background: linear-gradient(160deg, #111111 0%, #0E0E0E 100%);
  border: 1px solid #1E1E1E; border-radius: 18px;
  padding: 0; position: relative; overflow: hidden;
  transform-style: preserve-3d;
  transition: transform 200ms ease;
  box-shadow: 0 30px 80px rgba(0,212,255,0.08), 0 60px 120px rgba(0,0,0,0.6);
}
.hero-card-glare {
  position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, rgba(0,212,255,0.08), transparent 40%);
}
.hero-card-inner { padding: 22px; position: relative; z-index: 1; }
.hero-card-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 16px; padding-bottom: 14px;
  border-bottom: 1px solid #1E1E1E;
}
.hero-card-title {
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: #8A8A8A; letter-spacing: 0.05em;
}
.hero-card-foot {
  margin-top: 16px; padding-top: 14px;
  border-top: 1px solid #1E1E1E;
  display: flex; gap: 4px;
}
.convo { display: flex; flex-direction: column; gap: 10px; min-height: 220px; }
.convo-line {
  font-size: 14px; line-height: 1.5; min-height: 22px;
}
.convo-speaker {
  color: #00D4FF; font-weight: 600;
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  margin-right: 6px;
}
.convo-line[data-speaker="Caller"] .convo-speaker { color: #A78BFA; }
.convo-text {
  color: #F5F5F5;
}
.convo-text::after {
  content: "▎"; color: #00D4FF; margin-left: 2px;
  animation: caretBlink 1s steps(2) infinite;
}
.convo-text.done::after { display: none; }
@keyframes caretBlink {
  0%, 50% { opacity: 1; }
  50.01%, 100% { opacity: 0; }
}

/* ── sticky metric strip ────────────────────────────────────────── */
.metric-strip {
  position: sticky; top: 60px; z-index: 30;
  background: rgba(5,5,5,0.92);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-top: 1px solid #1E1E1E;
  border-bottom: 1px solid #1E1E1E;
}
.metric-strip-inner {
  max-width: 1240px; margin: 0 auto;
  padding: 14px 32px;
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
}
.metric-cell {
  text-align: center;
  border-right: 1px solid #1E1E1E;
  padding: 0 16px;
  display: flex; flex-direction: column; align-items: center; gap: 6px;
}
.metric-cell:last-child { border-right: none; }
.metric-num {
  font-size: 24px; color: #00D4FF; font-weight: 500;
  font-feature-settings: "tnum" 1; letter-spacing: -0.02em;
  display: inline-flex; align-items: center; gap: 8px;
}
.metric-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
  font-weight: 600;
}
.metric-circle-cell { padding: 4px 16px; }
.metric-circle {
  position: relative; width: 56px; height: 56px;
}
.metric-circle svg { width: 100%; height: 100%; }
.metric-circle svg circle {
  transition: stroke-dashoffset 1200ms cubic-bezier(.25,.8,.3,1);
}
.metric-circle-text {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; color: #F5F5F5;
}
.metric-sparkline { width: 80px; height: 16px; opacity: 0.85; }
.pulse-dot-large { width: 10px; height: 10px; }
@media (max-width: 720px) {
  .metric-strip-inner { grid-template-columns: repeat(2, 1fr); gap: 12px 0; padding: 10px 16px; }
  .metric-cell { border-right: none; }
}

/* ── agent showcase ─────────────────────────────────────────────── */
.agents-section { padding: 88px 0; }
.agent-showcase-grid {
  display: grid; gap: 24px;
  grid-template-columns: repeat(2, 1fr);
  perspective: 1500px;
}
@media (max-width: 900px) { .agent-showcase-grid { grid-template-columns: 1fr; } }

.agent-card-cinematic {
  background: linear-gradient(160deg, #111111 0%, #0E0E0E 100%);
  border: 1px solid #1E1E1E; border-radius: 20px;
  padding: 0; position: relative; overflow: hidden;
  transform-style: preserve-3d;
  transition: transform 220ms cubic-bezier(.25,.8,.3,1),
              box-shadow 320ms ease,
              border-color 320ms ease;
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 30px 60px rgba(0,0,0,0.4);
}
.agent-card-cinematic:hover {
  border-color: var(--accent-soft);
  box-shadow: 0 1px 0 rgba(255,255,255,0.06) inset,
              0 0 0 1px var(--accent-soft),
              0 30px 80px var(--accent-glow);
}
.agent-card-glare {
  position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(160deg, var(--accent-soft), transparent 35%);
  opacity: 0.7;
}
.agent-card-inner { padding: 28px; position: relative; z-index: 1; }
.agent-card-head { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; flex-wrap: wrap; }
.agent-avatar {
  width: 56px; height: 56px; border-radius: 50%;
  flex-shrink: 0;
  background: linear-gradient(135deg, var(--accent), rgba(255,255,255,0.05));
  display: inline-flex; align-items: center; justify-content: center;
  font-family: 'Syne', sans-serif; font-weight: 800; font-size: 24px;
  color: #050505;
  box-shadow: 0 0 0 2px rgba(255,255,255,0.06), 0 0 30px var(--accent-soft, rgba(0,212,255,0.3));
}
.agent-id { flex: 1; min-width: 0; }
.agent-id h3 { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 22px; margin: 0; }
.pulse-pill { white-space: nowrap; }
.agent-summary { font-size: 14px; margin: 0 0 18px; color: #F5F5F5; }
.agent-meta {
  display: grid; grid-template-columns: 96px 1fr; gap: 6px 14px;
  margin: 0 0 22px; font-size: 13px;
}
.agent-meta dt { color: #8A8A8A; }
.agent-meta dd { margin: 0; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
.agent-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.pulse-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #10B981; display: inline-block;
  box-shadow: 0 0 0 0 rgba(16,185,129,0.7);
  animation: pulse 1.6s infinite;
}
.pulse-dot-large {
  background: #10B981;
  box-shadow: 0 0 0 0 rgba(16,185,129,0.7);
  animation: pulse 1.6s infinite;
}

/* ── timeline ───────────────────────────────────────────────────── */
.timeline-section { padding: 64px 0; }
.timeline {
  display: grid; gap: 24px;
  grid-template-columns: repeat(3, 1fr);
  position: relative;
}
.timeline-rail {
  position: absolute; top: 78px; left: 12%; right: 12%;
  height: 2px;
  background: linear-gradient(90deg, transparent, rgba(0,212,255,0.4), rgba(0,212,255,0.4), transparent);
  pointer-events: none;
}
@media (max-width: 900px) {
  .timeline { grid-template-columns: 1fr; }
  .timeline-rail { display: none; }
}
.timeline-step {
  background: #111111; border: 1px solid #1E1E1E;
  border-radius: 16px; padding: 28px;
  position: relative;
}
.timeline-dot {
  position: absolute; top: -10px; left: 28px;
  width: 18px; height: 18px; border-radius: 50%;
  background: #00D4FF; box-shadow: 0 0 0 4px #050505, 0 0 20px rgba(0,212,255,0.6);
}
.timeline-num { color: #00D4FF; font-size: 13px; letter-spacing: 0.1em; font-weight: 600; }
.timeline-icon {
  width: 44px; height: 44px; margin: 14px 0 18px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid #2A2A2A; border-radius: 12px;
  color: #00D4FF; background: #0E0E0E;
}
.timeline-icon svg { width: 22px; height: 22px; }
.timeline-step h3 { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 18px; margin: 0 0 8px; }

/* ── features ───────────────────────────────────────────────────── */
.features-section { padding: 88px 0; }
.features-grid {
  display: grid; gap: 16px;
  grid-template-columns: repeat(3, 1fr);
}
@media (max-width: 1000px) { .features-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 540px)  { .features-grid { grid-template-columns: 1fr; } }
.feature-card-cinematic {
  position: relative; overflow: hidden;
  background: #111111; border: 1px solid #1E1E1E;
  border-radius: 14px; padding: 24px;
  transition: border-color 220ms ease, transform 220ms ease, box-shadow 220ms ease;
}
.feature-card-cinematic:hover {
  border-color: rgba(0,212,255,0.45);
  transform: translateY(-3px);
  box-shadow: 0 0 0 1px rgba(0,212,255,0.25), 0 18px 44px rgba(0,212,255,0.12);
}
.feature-card-glow {
  position: absolute; inset: 0; pointer-events: none; opacity: 0;
  background: radial-gradient(280px 180px at 50% 0%, rgba(0,212,255,0.15), transparent 70%);
  transition: opacity 220ms ease;
}
.feature-card-cinematic:hover .feature-card-glow { opacity: 1; }
.feature-icon {
  width: 38px; height: 38px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid #2A2A2A; border-radius: 10px;
  color: #00D4FF; background: #0E0E0E;
  margin-bottom: 14px; position: relative; z-index: 1;
}
.feature-icon svg { width: 18px; height: 18px; }
.feature-card-cinematic h3 {
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 16px;
  margin: 0 0 8px; position: relative; z-index: 1;
}
.feature-card-cinematic p { font-size: 13px; margin: 0; position: relative; z-index: 1; }

/* ── phone demo ─────────────────────────────────────────────────── */
.phone-demo { padding: 88px 0; }
.phone-stage {
  display: grid; grid-template-columns: 360px 1fr; gap: 56px;
  align-items: center; max-width: 1000px; margin: 0 auto;
}
@media (max-width: 900px) { .phone-stage { grid-template-columns: 1fr; gap: 32px; } }

.phone {
  width: 320px; height: 640px; margin: 0 auto;
  position: relative; perspective: 1200px;
}
.phone-frame {
  width: 100%; height: 100%;
  background: linear-gradient(180deg, #0a0a0a, #050505);
  border-radius: 44px;
  border: 1px solid #1E1E1E;
  padding: 12px;
  box-shadow:
    0 0 0 4px #1E1E1E,
    0 30px 80px rgba(0,212,255,0.18),
    0 60px 120px rgba(0,0,0,0.7);
  position: relative; overflow: hidden;
  transform: rotateY(-4deg) rotateX(2deg);
}
.phone-notch {
  position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
  width: 110px; height: 22px; background: #050505;
  border-radius: 0 0 14px 14px; z-index: 2;
}
.phone-screen {
  width: 100%; height: 100%; border-radius: 32px;
  background: linear-gradient(180deg, #050505 0%, #0E0E0E 100%);
  padding: 36px 16px 16px; display: flex; flex-direction: column; gap: 12px;
}
.phone-status { color: #5F5F5F; text-align: center; padding: 4px 0 8px; letter-spacing: 0.1em; }
.phone-callee {
  display: flex; align-items: center; gap: 10px;
  padding: 12px; border: 1px solid #1E1E1E; border-radius: 12px;
  background: #0E0E0E;
}
.phone-callee .agent-avatar { width: 36px; height: 36px; font-size: 16px; }
.phone-callee-id { flex: 1; min-width: 0; }
.phone-name { font-weight: 600; font-size: 14px; }
.phone-role { font-size: 11px; }
.phone-convo {
  flex: 1; display: flex; flex-direction: column; gap: 8px;
  overflow: hidden;
}
.phone-line {
  font-size: 12px; line-height: 1.45;
  padding: 8px 12px; border-radius: 14px; max-width: 80%;
  word-wrap: break-word;
}
.phone-line-agent {
  background: rgba(0,212,255,0.12); color: #F5F5F5;
  border: 1px solid rgba(0,212,255,0.22);
  align-self: flex-start;
}
.phone-line-caller {
  background: #161616; color: #F5F5F5;
  border: 1px solid #1E1E1E;
  align-self: flex-end;
}
.phone-line .convo-text::after { font-size: 11px; }
.phone-controls { padding-top: 8px; }
.phone-play {
  width: 100%; padding: 12px; border-radius: 999px;
  background: #00D4FF; color: #001218; border: none;
  font-family: inherit; font-weight: 600; font-size: 13px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 10px;
  transition: filter 200ms ease, transform 120ms ease;
}
.phone-play:hover { filter: brightness(1.05); }
.phone-play:active { transform: translateY(1px); }
.phone-play[data-playing="true"] .phone-play-icon::before { content: "❚❚"; }
.phone-play[data-playing="true"] .phone-play-icon { content: ""; }

.phone-sidecar h3 {
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 24px; margin: 0 0 12px;
}
.phone-sidecar p { font-size: 14px; margin: 0 0 20px; }
.phone-cta-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 14px 16px; border: 1px solid #1E1E1E; border-radius: 12px;
  background: #0E0E0E; margin-bottom: 24px;
}
.phone-number { font-size: 16px; color: #00D4FF; }
.phone-feature-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.phone-feature {
  padding: 12px; border: 1px solid #1E1E1E; border-radius: 12px;
  background: #0E0E0E; text-align: center;
}
.phone-feature strong { display: block; color: #00D4FF; font-size: 18px; margin-bottom: 4px; }

/* ── reveal-on-scroll ───────────────────────────────────────────── */
.reveal {
  opacity: 0; transform: translateY(24px);
  transition: opacity 700ms cubic-bezier(.2,.8,.2,1) var(--reveal-delay, 0ms),
              transform 700ms cubic-bezier(.2,.8,.2,1) var(--reveal-delay, 0ms);
}
.reveal.visible { opacity: 1; transform: translateY(0); }

/* ── data collapse ──────────────────────────────────────────────── */
.dashboard-anchor { padding: 64px 0 0; scroll-margin-top: 132px; }
.data-collapse {
  border: 1px solid #1E1E1E; border-radius: 14px;
  background: #111111; padding: 0; overflow: hidden;
}
.data-collapse summary {
  cursor: pointer; padding: 18px 22px;
  display: flex; align-items: center; justify-content: space-between;
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 14px;
  text-transform: uppercase; letter-spacing: 0.08em;
  list-style: none;
  border-bottom: 1px solid transparent;
  transition: border-color 200ms ease;
}
.data-collapse summary::-webkit-details-marker { display: none; }
.data-collapse[open] summary { border-bottom-color: #1E1E1E; }
.data-collapse-icon { color: #00D4FF; font-size: 14px; transition: transform 200ms ease; }
.data-collapse[open] .data-collapse-icon { transform: rotate(180deg); }
.data-collapse-body { padding: 0; }
.data-collapse-body .panel { border: none; border-radius: 0; margin-bottom: 0; }
.data-collapse-body .panel-head { display: none; }

/* ── footer ─────────────────────────────────────────────────────── */
.landing-footer {
  border-top: 1px solid #1E1E1E;
  padding: 56px 0 32px;
  margin-top: 32px;
  background: #050505;
}
.footer-inner {
  max-width: 1240px; margin: 0 auto; padding: 0 32px;
  display: grid; grid-template-columns: 1.5fr 2fr; gap: 32px;
  align-items: flex-start;
}
@media (max-width: 720px) { .footer-inner { grid-template-columns: 1fr; } }
.footer-brand { display: flex; align-items: baseline; gap: 12px; }
.brand-mark.large { font-size: 28px; }
.footer-cols {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px;
}
@media (max-width: 540px) { .footer-cols { grid-template-columns: 1fr 1fr; } }
.footer-col h4 {
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
  color: #8A8A8A; margin: 0 0 12px;
}
.footer-col a, .footer-col span {
  display: block; color: #F5F5F5; text-decoration: none; font-size: 13px;
  margin-bottom: 8px;
}
.footer-col a:hover { color: #00D4FF; }
.footer-col span.dim { color: #5F5F5F; }

/* anchor scroll-margin so sticky nav + metric strip don't cover headings */
#agents, #timeline, #features, #dashboard, #phone-demo {
  scroll-margin-top: 140px;
}

/* ── print stylesheet ───────────────────────────────────────────── */
@media print {
  @page { margin: 14mm; }
  body { background: white !important; color: #111 !important; }
  .cynea-loader, .hero-bg, .hero-orb, .hero-scanline, .pulse-dot, .pulse-dot-large,
  .hero-card-glare, .agent-card-glare, .feature-card-glow,
  .nav-links, .btn, #toast, #audio-player {
    display: none !important;
  }
  body.landing main.landing-main { animation: none !important; opacity: 1 !important; }
  .landing-nav { position: static !important; background: white !important; border-color: #ddd !important; }
  .metric-strip { position: static !important; background: white !important; border-color: #ddd !important; }
  .hero-line-1, .hero-line-2 { color: #111 !important; -webkit-text-fill-color: #111 !important; }
  .agent-card-cinematic, .feature-card-cinematic, .timeline-step, .phone-frame,
  .data-collapse, .hero-card {
    background: white !important; border-color: #ddd !important; box-shadow: none !important;
    color: #111 !important;
  }
  .muted, .dim, .section-eyebrow, .timeline-num, .phone-number {
    color: #555 !important;
  }
  .reveal { opacity: 1 !important; transform: none !important; }
}

/* ── interactive chat widget ────────────────────────────────────── */
.chat-demo-section { padding: 88px 0; }
.chat-demo {
  width: 100%; max-width: 375px; height: 700px;
  margin: 0 auto;
  background: #1a1a1a;
  border: 1px solid #1E1E1E;
  border-radius: 24px;
  display: flex; flex-direction: column;
  overflow: hidden;
  box-shadow:
    0 0 0 1px rgba(255,255,255,0.03) inset,
    0 30px 80px rgba(0,212,255,0.10),
    0 60px 120px rgba(0,0,0,0.6);
}
@media (max-width: 540px) {
  .chat-demo { height: 620px; max-width: 100%; border-radius: 20px; }
}

.chat-header {
  display: flex; align-items: center; gap: 10px;
  padding: 16px 18px;
  background: #0E0E0E;
  border-bottom: 1px solid #1E1E1E;
}
.chat-header-id { line-height: 1.2; }
.chat-header-title {
  font-family: 'Syne', sans-serif; font-weight: 700; font-size: 14px;
  color: #F5F5F5;
}
.chat-header-status { font-size: 11px; }

.chat-messages {
  flex: 1; min-height: 0;
  overflow-y: auto;
  padding: 16px;
  display: flex; flex-direction: column; gap: 10px;
  scroll-behavior: smooth;
  scrollbar-width: thin; scrollbar-color: #2A2A2A transparent;
}
.chat-messages::-webkit-scrollbar { width: 6px; }
.chat-messages::-webkit-scrollbar-thumb { background: #2A2A2A; border-radius: 999px; }

.chat-msg {
  max-width: 82%;
  padding: 10px 14px; border-radius: 16px;
  font-size: 13px; line-height: 1.45;
  position: relative;
  animation: chatMsgIn 240ms cubic-bezier(.2,.8,.2,1);
  word-wrap: break-word;
}
@keyframes chatMsgIn {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
.chat-msg-kwame {
  align-self: flex-start;
  background: #2a2a2a; color: #F5F5F5;
  border: 1px solid #1E1E1E;
  border-bottom-left-radius: 4px;
  padding-right: 36px;
}
.chat-msg-user {
  align-self: flex-end;
  background: #00D4FF; color: #001218;
  border-bottom-right-radius: 4px;
  font-weight: 500;
}
.chat-msg-replay {
  position: absolute; right: 6px; bottom: 6px;
  width: 22px; height: 22px; border-radius: 50%;
  background: rgba(0,212,255,0.15); color: #00D4FF;
  border: none; cursor: pointer;
  font-size: 9px; line-height: 1;
  display: inline-flex; align-items: center; justify-content: center;
  transition: background 120ms ease;
}
.chat-msg-replay:hover { background: rgba(0,212,255,0.30); }
.chat-msg-replay[data-playing="true"] { background: #00D4FF; color: #001218; }

.chat-typing {
  align-self: flex-start;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 12px 14px;
  background: #2a2a2a; border: 1px solid #1E1E1E;
  border-radius: 16px;
}
.chat-typing span {
  width: 6px; height: 6px; border-radius: 50%;
  background: #8A8A8A;
  animation: chatTypePulse 1.2s infinite;
}
.chat-typing span:nth-child(2) { animation-delay: 0.18s; }
.chat-typing span:nth-child(3) { animation-delay: 0.36s; }
@keyframes chatTypePulse {
  0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
  30%           { opacity: 1;   transform: translateY(-3px); }
}

.chat-chips {
  padding: 10px 14px 0;
  display: flex; gap: 8px; overflow-x: auto;
  scrollbar-width: none;
}
.chat-chips::-webkit-scrollbar { display: none; }
.chat-chip {
  flex-shrink: 0;
  padding: 6px 12px; border-radius: 999px;
  background: transparent; color: #00D4FF;
  border: 1px solid rgba(0,212,255,0.40);
  font-family: inherit; font-size: 12px;
  cursor: pointer; white-space: nowrap;
  transition: background 150ms ease, border-color 150ms ease;
}
.chat-chip:hover { background: rgba(0,212,255,0.12); border-color: #00D4FF; }
.chat-chip:active { transform: translateY(1px); }

.chat-input-row {
  display: flex; gap: 8px;
  padding: 12px 14px 14px;
  border-top: 1px solid #1E1E1E;
  margin-top: 10px;
}
.chat-input {
  flex: 1; min-width: 0;
  background: #0E0E0E; color: #F5F5F5;
  border: 1px solid #1E1E1E; border-radius: 999px;
  padding: 10px 16px;
  font-family: inherit; font-size: 13px;
  outline: none;
  transition: border-color 150ms ease, box-shadow 150ms ease;
}
.chat-input:focus {
  border-color: #00D4FF;
  box-shadow: 0 0 0 3px rgba(0,212,255,0.15);
}
.chat-input::placeholder { color: #5F5F5F; }
.chat-send {
  width: 40px; height: 40px; border-radius: 50%;
  background: #00D4FF; color: #001218;
  border: none; cursor: pointer;
  font-size: 18px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
  transition: filter 120ms ease, transform 120ms ease;
}
.chat-send:hover { filter: brightness(1.05); }
.chat-send:active { transform: translateY(1px); }
.chat-send:disabled { opacity: 0.4; cursor: not-allowed; }

.chat-autoplay-hint {
  align-self: center; max-width: 90%;
  padding: 8px 12px; border-radius: 12px;
  background: rgba(0,212,255,0.08);
  border: 1px dashed rgba(0,212,255,0.35);
  color: #00D4FF;
  font-size: 11px; text-align: center;
}

/* Print: collapse the widget; the dashboard table tells the story */
@media print {
  .chat-demo-section { display: none !important; }
}

/* ── polish layer ───────────────────────────────────────────────── */
/* All animations below honour prefers-reduced-motion via the global
   override at the top of this stylesheet. New keyframes use only
   transform + opacity so the GPU compositor can drive them at 60fps. */

/* scroll progress bar — fixed 2px line at the very top */
#cynea-scroll-progress {
  position: fixed; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, #00D4FF 0%, #A78BFA 100%);
  width: 0%;
  z-index: 60;
  pointer-events: none;
  transition: width 80ms linear;
  box-shadow: 0 0 8px rgba(0,212,255,0.6);
}

/* cursor glow — desktop only; CSS hides it on touch / coarse pointer */
#cynea-cursor-glow {
  position: fixed; top: 0; left: 0;
  width: 480px; height: 480px;
  margin-left: -240px; margin-top: -240px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(0,212,255,0.10) 0%, rgba(0,212,255,0) 60%);
  pointer-events: none;
  z-index: 1;
  opacity: 0;
  transform: translate3d(50vw, 50vh, 0);
  transition: opacity 200ms ease;
  mix-blend-mode: screen;
}
@media (hover: hover) and (pointer: fine) {
  #cynea-cursor-glow.active { opacity: 1; }
}
@media (hover: none), (pointer: coarse), (max-width: 720px) {
  #cynea-cursor-glow { display: none !important; }
}

/* back-to-top button */
#cynea-back-to-top {
  position: fixed; right: 22px; bottom: 22px;
  width: 44px; height: 44px; border-radius: 50%;
  background: #00D4FF; color: #001218;
  border: none; cursor: pointer;
  font-size: 18px; line-height: 1;
  display: inline-flex; align-items: center; justify-content: center;
  z-index: 55;
  opacity: 0; transform: translate3d(0, 14px, 0);
  transition: opacity 220ms ease, transform 220ms ease, filter 120ms ease;
  box-shadow: 0 0 0 1px rgba(0,212,255,0.45), 0 12px 36px rgba(0,212,255,0.35);
}
#cynea-back-to-top[hidden] { display: none; }
#cynea-back-to-top.visible { opacity: 1; transform: translate3d(0, 0, 0); }
#cynea-back-to-top:hover { filter: brightness(1.06); transform: translate3d(0, -2px, 0); }
#cynea-back-to-top:active { transform: translate3d(0, 0, 0); }

/* nav underline-slide on hover */
.nav-links a {
  position: relative;
  padding-bottom: 2px;
}
.nav-links a::after {
  content: ""; position: absolute; left: 0; bottom: -2px;
  width: 100%; height: 1px;
  background: #00D4FF;
  transform: scaleX(0); transform-origin: left center;
  transition: transform 260ms cubic-bezier(.2,.8,.2,1);
}
.nav-links a:hover::after { transform: scaleX(1); }

/* button micro-interactions — subtle scale + lift on hover */
.btn { transition: transform 180ms cubic-bezier(.2,.8,.2,1), box-shadow 220ms ease, filter 120ms ease, background 200ms ease; }
.btn:hover { transform: translateY(-1px) scale(1.025); }
.btn:active { transform: translateY(0) scale(1); }

/* brand glow pulse — only fires on the static (post-loader) wordmark */
@keyframes brandGlowPulse {
  0%, 100% { text-shadow: 0 0 18px rgba(0,212,255,0.4); }
  50%      { text-shadow: 0 0 30px rgba(0,212,255,0.75), 0 0 60px rgba(0,212,255,0.25); }
}
.landing-nav .brand-mark,
.landing-footer .brand-mark.large {
  animation: brandGlowPulse 4.5s ease-in-out infinite;
}

/* hero floating-card subtle glow on the typewritten text */
.hero-card .convo-text { text-shadow: 0 0 12px rgba(0,212,255,0.18); }

/* agent-card border shimmer — a slow conic sweep behind the card */
.agent-card-cinematic::before {
  content: ""; position: absolute; inset: -1px;
  border-radius: inherit;
  background: conic-gradient(
    from 0deg,
    transparent 0deg,
    var(--accent-soft) 60deg,
    transparent 120deg,
    transparent 360deg
  );
  opacity: 0;
  transition: opacity 400ms ease;
  z-index: 0;
  pointer-events: none;
  animation: agentShimmer 8s linear infinite;
}
.agent-card-cinematic:hover::before { opacity: 0.5; }
@keyframes agentShimmer {
  to { transform: rotate(360deg); }
}

/* metrics strip — subtle background pattern */
.metric-strip-inner::before {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background-image:
    linear-gradient(rgba(0,212,255,0.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,255,0.025) 1px, transparent 1px);
  background-size: 24px 24px;
  opacity: 0.5;
}
.metric-strip { position: sticky; }
.metric-strip-inner { position: relative; }
.metric-num { font-weight: 600; }

/* features — glass-morphism + icon hover bounce */
.feature-card-cinematic {
  background: linear-gradient(160deg, rgba(17,17,17,0.85) 0%, rgba(14,14,14,0.85) 100%);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
}
.feature-icon { transition: transform 320ms cubic-bezier(.2,.8,.2,1); }
.feature-card-cinematic:hover .feature-icon { transform: rotate(-6deg) scale(1.08); }

/* phone mockup — top reflection highlight */
.phone-frame::before {
  content: ""; position: absolute; inset: 0;
  border-radius: inherit;
  background: linear-gradient(180deg, rgba(255,255,255,0.06) 0%, rgba(255,255,255,0) 25%);
  pointer-events: none;
  z-index: 3;
}

/* hero parallax — applied via JS by setting --parallax-y on .hero-bg */
.hero-bg { will-change: transform; transform: translate3d(0, var(--parallax-y, 0px), 0); }

/* reveal stagger — child cards within a section fade up one after another */
.agent-showcase-grid > .reveal:nth-child(1) { transition-delay: 0ms; }
.agent-showcase-grid > .reveal:nth-child(2) { transition-delay: 120ms; }
.features-grid > .reveal:nth-child(1) { transition-delay: 0ms; }
.features-grid > .reveal:nth-child(2) { transition-delay: 80ms; }
.features-grid > .reveal:nth-child(3) { transition-delay: 160ms; }
.features-grid > .reveal:nth-child(4) { transition-delay: 240ms; }
.features-grid > .reveal:nth-child(5) { transition-delay: 320ms; }
.features-grid > .reveal:nth-child(6) { transition-delay: 400ms; }

/* reveal — add a subtle scale-in on top of the existing translateY */
.reveal { transform: translateY(24px) scale(0.985); }
.reveal.visible { transform: translateY(0) scale(1); }

/* online dot — slightly more natural pulse cadence */
.pulse-dot {
  animation: pulseDotNatural 1.8s cubic-bezier(.4,0,.6,1) infinite;
}
@keyframes pulseDotNatural {
  0%   { box-shadow: 0 0 0 0 rgba(16,185,129,0.7); }
  60%  { box-shadow: 0 0 0 9px rgba(16,185,129,0); }
  100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); }
}

/* hide all polish layers on print */
@media print {
  #cynea-scroll-progress, #cynea-cursor-glow, #cynea-back-to-top { display: none !important; }
  .agent-card-cinematic::before, .phone-frame::before, .metric-strip-inner::before { display: none !important; }
  .feature-card-cinematic { backdrop-filter: none !important; }
}
"""


# ------------------------------------------------------------------
# JS — landing-only handlers, layered on top of dashboard JS
# ------------------------------------------------------------------

def _landing_render_js(calls: list) -> str:
    base = _render_js(calls)
    extras = r"""
// ====================================================================
// Cinematic landing JS
// ====================================================================

// -- loader cleanup ---------------------------------------------------
(function() {
  const loader = document.getElementById('cyneaLoader');
  if (!loader) return;
  let removed = false;
  const remove = () => {
    if (removed) return;
    removed = true;
    loader.classList.add('done');
  };
  loader.addEventListener('animationend', (e) => {
    if (e.animationName === 'loaderFade') remove();
  });
  // Safety net: never let the loader stick around after 3 s.
  setTimeout(remove, 3000);
})();

// -- shared playDemo ------------------------------------------------
function playDemo(audioFile, fallback, errorMsg) {
  const player = document.getElementById('audio-player');
  if (!player) { showToast(errorMsg || 'Audio player not available', 3000); return; }
  if (!audioFile) { showToast(errorMsg || 'No audio configured', 3000); return; }

  // Clear any "playing" state from previous buttons.
  document.querySelectorAll('.phone-play, .play-btn').forEach(b => b.removeAttribute('data-playing'));

  const tryPlay = (src, onFail) => {
    player.src = src;
    let bailed = false;
    const fail = () => { if (bailed) return; bailed = true; onFail(); };
    const onError = () => { player.removeEventListener('error', onError); fail(); };
    player.addEventListener('error', onError, { once: true });
    const playPromise = player.play();
    if (playPromise && typeof playPromise.catch === 'function') {
      playPromise.catch(() => fail());
    }
  };

  try { player.pause(); player.currentTime = 0; } catch (e) {}
  tryPlay(audioFile, () => {
    if (fallback) tryPlay(fallback, () => showToast(errorMsg || 'Audio not available', 3000));
    else showToast(errorMsg || 'Audio not available', 3000);
  });
}

// Wire every play-btn (hero CTA, agent cards, phone demo) to playDemo.
document.querySelectorAll('.play-btn').forEach(btn => {
  btn.addEventListener('click', (ev) => {
    ev.preventDefault();
    btn.setAttribute('data-playing', 'true');
    playDemo(
      btn.getAttribute('data-audio'),
      btn.getAttribute('data-audio-fallback'),
      btn.getAttribute('data-error') || ((btn.getAttribute('data-label') || 'Demo') + ' audio not available')
    );
  });
});

// Reset any per-button "playing" UI when audio ends or pauses.
(function() {
  const player = document.getElementById('audio-player');
  if (!player) return;
  const clearPlaying = () => {
    document.querySelectorAll('[data-playing]').forEach(b => b.removeAttribute('data-playing'));
  };
  ['ended', 'pause'].forEach(ev => player.addEventListener(ev, clearPlaying));
})();

// -- 3D card tilt -----------------------------------------------------
(function() {
  const cards = document.querySelectorAll('[data-tilt]');
  if (!cards.length) return;
  cards.forEach(card => {
    let rect = null;
    const update = () => { rect = card.getBoundingClientRect(); };
    card.addEventListener('mouseenter', update);
    card.addEventListener('mousemove', (e) => {
      if (!rect) update();
      const x = ((e.clientX - rect.left) / rect.width  - 0.5) * 2;  // -1..1
      const y = ((e.clientY - rect.top)  / rect.height - 0.5) * 2;
      card.style.transform = `perspective(1100px) rotateY(${x * 5}deg) rotateX(${-y * 5}deg) translateZ(0)`;
    });
    card.addEventListener('mouseleave', () => {
      card.style.transform = 'perspective(1100px) rotateY(0deg) rotateX(0deg)';
      rect = null;
    });
  });
})();

// -- typewriter -------------------------------------------------------
function startTypewriter(container, opts) {
  const lines = container.querySelectorAll('[data-text]');
  if (!lines.length) return;
  const speed = (opts && opts.speed)   || 26;   // ms per char
  const linePause = (opts && opts.linePause) || 700;
  const loopPause = (opts && opts.loopPause) || 3500;
  let i = 0;

  function buildLine(line) {
    const speaker = line.dataset.speaker || '';
    line.innerHTML =
      (speaker ? '<span class="convo-speaker">' + speaker + ':</span> ' : '') +
      '<span class="convo-text"></span>';
    return line.querySelector('.convo-text');
  }

  function nextLine() {
    if (i >= lines.length) {
      // Loop the conversation.
      setTimeout(() => {
        lines.forEach(l => l.innerHTML = '');
        i = 0;
        nextLine();
      }, loopPause);
      return;
    }
    const target = buildLine(lines[i]);
    const text = lines[i].dataset.text || '';
    let j = 0;
    function typeChar() {
      if (j >= text.length) {
        target.classList.add('done');
        i++;
        setTimeout(nextLine, linePause);
        return;
      }
      target.textContent += text[j++];
      setTimeout(typeChar, speed);
    }
    typeChar();
  }
  nextLine();
}

// Trigger each typewriter container the first time it scrolls into view.
(function() {
  const containers = document.querySelectorAll('[data-typewriter]');
  if (!containers.length) return;
  if (!('IntersectionObserver' in window)) {
    containers.forEach(c => startTypewriter(c));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      io.unobserve(e.target);
      startTypewriter(e.target);
    });
  }, { threshold: 0.25 });
  containers.forEach(c => io.observe(c));
})();

// -- reveal-on-scroll -------------------------------------------------
(function() {
  const els = document.querySelectorAll('.reveal');
  if (!els.length) return;
  if (!('IntersectionObserver' in window)) {
    els.forEach(el => el.classList.add('visible'));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        e.target.classList.add('visible');
        io.unobserve(e.target);
      }
    });
  }, { threshold: 0.15 });
  els.forEach(el => io.observe(el));
})();

// -- counter-on-scroll for the metric strip --------------------------
(function() {
  const counters = document.querySelectorAll('[data-counter]');
  const circles = document.querySelectorAll('[data-progress-target]');
  if (!counters.length && !circles.length) return;
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

  const animateCircle = (el) => {
    const target = parseFloat(el.getAttribute('data-progress-target') || '0');
    // Trigger transition by reading layout, then writing the target value.
    el.getBoundingClientRect();
    el.setAttribute('stroke-dashoffset', target.toFixed(2));
  };

  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting && !fired.has(entry.target)) {
          fired.add(entry.target);
          if (entry.target.hasAttribute('data-counter')) animateCounter(entry.target);
          if (entry.target.hasAttribute('data-progress-target')) animateCircle(entry.target);
        }
      });
    }, { threshold: 0.4 });
    counters.forEach(el => io.observe(el));
    circles.forEach(el => io.observe(el));
  } else {
    counters.forEach(animateCounter);
    circles.forEach(animateCircle);
  }
})();

// -- interactive chat widget ------------------------------------------
//
// Stateful flow machine. Each FLOW is an ordered list of steps; each
// step has its own keywords, response text, audio file, and chip set
// to show after the response. The router first tries to advance the
// active flow; if the user goes off-script, it exits the flow and
// re-routes from the top.
//
// Concurrency guard: the input + send button disable while a turn is
// in progress (typing indicator + typewriter). Submits during that
// window are dropped silently — preferable to queuing because the
// user almost always meant to amend their previous message, not stack
// a second one.

(function() {
  const messagesEl = document.getElementById('chat-messages');
  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  const send = document.getElementById('chat-send');
  const chipsEl = document.getElementById('chat-chips');
  const player = document.getElementById('audio-player');
  if (!messagesEl || !form || !input || !chipsEl) return;

  // ── Flow definitions ──────────────────────────────────────────────
  // chips_after[i] is the chip set shown AFTER the response from
  // step[i]. Length must equal steps.length.
  const FLOWS = {
    booking: {
      chips_after: [
        ["This Friday, deluxe room", "Saturday, standard please", "What about cancellations?"],
        ["Yes please, hold it", "Maybe later", "Tell me about breakfast"],
        [],   // step 2 is the closer — let the user respond freely
      ],
      steps: [
        {
          keywords: ['book', 'booking', 'reserve', 'reservation', 'room', 'rooms',
                     'check in', 'check-in', 'stay', 'night', 'nights'],
          text: "Ah, lovely! When would you like to check in? We have standard at $80, deluxe at $120, and executive suites at $200 per night.",
          audio: "kwame_test_2.mp3",
        },
        {
          keywords: ['friday','saturday','sunday','monday','tuesday','wednesday','thursday',
                     'tonight','tomorrow','next week','this weekend','weekend',
                     'standard','deluxe','executive','suite','single','double',
                     'this week'],
          text: "Let me check... yes, we have a deluxe room available this Friday. That's $120 per night, breakfast included. Shall I hold the booking for you?",
          audio: "kwame_test_3.mp3",
        },
        {
          keywords: ['yes','please','confirm','go ahead','sure','ok','okay','yep',
                     'alright','do it','book it','hold it','hold the',"let's do it"],
          text: "Perfect. I'll need your full name, phone number, and email address. And a deposit equal to the first night to secure the booking.",
          audio: null,
        },
      ],
    },
    pricing: {
      chips_after: [["I'd like to book a room", "Do you have a pool?", "What's included with breakfast?"]],
      steps: [
        {
          keywords: ['rate','rates','price','prices','cost','how much','dollar','dollars',
                     'cedis','expensive','cheap','rooms cost'],
          text: "Our standard room is $80 per night, deluxe is $120, and the executive suite with ocean view is $200. All include complimentary breakfast and WiFi.",
          audio: null,
        },
      ],
    },
    amenities: {
      chips_after: [["I'd like to book a room", "What are your rates?", "What time is breakfast?"]],
      steps: [
        {
          keywords: ['pool','amenity','amenities','facility','facilities','wifi',
                     'gym','fitness','restaurant','shuttle','airport','have a',
                     'do you have'],
          text: "Yes! We have a swimming pool open from 7am to 9pm, a restaurant serving Ghanaian and continental dishes, free WiFi throughout, a fitness center, and airport shuttle service for $25 each way.",
          audio: null,
        },
      ],
    },
    complaint: {
      chips_after: [[]],
      steps: [
        {
          keywords: ['complaint','complaints','unhappy','problem','problems','dirty',
                     'rude','angry','broken','cold','noisy','disgusting','terrible',
                     'awful','worst','disappointed','frustrated','upset',
                     'manager','speak to'],
          text: "I'm really sorry to hear that. That's not the experience we want for our guests. Let me connect you with my manager right away to resolve this. What's the best number to reach you?",
          audio: null,
        },
      ],
    },
  };

  const DEFAULT_RESPONSE = {
    text: "Let me check on that for you. Is there anything else I can help with in the meantime?",
    audio: null,
  };

  const GREETING = {
    text: "Hello? Yes, Adinkra Hotel. Kwame speaking. How can I help you today?",
    audio: "kwame_test_1.mp3",
    chips: null,  // we use INITIAL_CHIPS for the opener
  };

  const INITIAL_CHIPS = [
    "I'd like to book a room",
    "What are your rates?",
    "Do you have a pool?",
    "I have a complaint",
  ];

  // ── Mutable state ─────────────────────────────────────────────────
  let typewriterTimer = null;
  let activeText = null;
  let activeFinalText = null;
  let activeChipsAfter = null;   // chips to install when typewriter completes
  let firstUserInteraction = true;
  let autoplayHintShown = false;
  let isProcessing = false;
  let conversationFlow = null;   // null | 'booking' | 'pricing' | ...
  let conversationStep = 0;

  // ── Routing ───────────────────────────────────────────────────────
  function routeMessage(text) {
    const t = (text || '').toLowerCase();

    // 1. Mid-flow: try to advance to the next step.
    if (conversationFlow) {
      const flow = FLOWS[conversationFlow];
      const nextIdx = conversationStep + 1;
      const nextStep = flow.steps[nextIdx];
      if (nextStep && nextStep.keywords.some((k) => t.includes(k))) {
        conversationStep = nextIdx;
        const isLast = nextIdx === flow.steps.length - 1;
        const response = Object.assign({}, nextStep, {
          chips: (flow.chips_after[nextIdx] && flow.chips_after[nextIdx].length)
                  ? flow.chips_after[nextIdx]
                  : INITIAL_CHIPS,
        });
        if (isLast) { conversationFlow = null; conversationStep = 0; }
        return response;
      }
      // Off-script — exit the flow and re-route from the top.
      conversationFlow = null;
      conversationStep = 0;
    }

    // 2. Top-level routing — pick a flow whose first step matches.
    for (const name in FLOWS) {
      if (!Object.prototype.hasOwnProperty.call(FLOWS, name)) continue;
      const flow = FLOWS[name];
      const firstStep = flow.steps[0];
      if (firstStep.keywords.some((k) => t.includes(k))) {
        conversationFlow = (flow.steps.length > 1) ? name : null;
        conversationStep = 0;
        return Object.assign({}, firstStep, {
          chips: (flow.chips_after[0] && flow.chips_after[0].length)
                  ? flow.chips_after[0]
                  : INITIAL_CHIPS,
        });
      }
    }

    // 3. No match — default response, keep state.
    return Object.assign({}, DEFAULT_RESPONSE, { chips: INITIAL_CHIPS });
  }

  // ── Chips ─────────────────────────────────────────────────────────
  function setActiveChips(items) {
    chipsEl.innerHTML = '';
    (items || []).forEach((msg) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chat-chip';
      btn.dataset.message = msg;
      btn.textContent = msg;
      btn.addEventListener('click', () => sendUserMessage(msg));
      chipsEl.appendChild(btn);
    });
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function finalisePendingTypewriter() {
    // Always called from inside appendKwameMessage() right before it
    // starts a new typewriter, so we DO NOT release the busy lock —
    // the upcoming tick() loop owns that. We do flush any pending
    // chip-update so the prior message's chips don't get clobbered.
    if (!typewriterTimer) return;
    clearTimeout(typewriterTimer);
    typewriterTimer = null;
    if (activeText && activeFinalText !== null) {
      activeText.textContent = activeFinalText;
    }
    if (activeChipsAfter) {
      setActiveChips(activeChipsAfter);
      activeChipsAfter = null;
    }
    activeText = null;
    activeFinalText = null;
  }

  function appendUserMessage(text) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-msg chat-msg-user';
    bubble.textContent = text;
    messagesEl.appendChild(bubble);
    scrollToBottom();
  }

  function appendKwameMessage(response, opts) {
    finalisePendingTypewriter();
    const allowAutoplay = !(opts && opts.skipAutoplay);
    const bubble = document.createElement('div');
    bubble.className = 'chat-msg chat-msg-kwame';
    const textEl = document.createElement('span');
    textEl.className = 'chat-msg-text';
    bubble.appendChild(textEl);

    // Replay button — shows up after the typewriter finishes.
    let replayBtn = null;
    if (response.audio) {
      replayBtn = document.createElement('button');
      replayBtn.type = 'button';
      replayBtn.className = 'chat-msg-replay';
      replayBtn.title = 'Replay audio';
      replayBtn.setAttribute('aria-label', 'Replay audio');
      replayBtn.dataset.audio = response.audio;
      replayBtn.textContent = '▶';
      replayBtn.style.opacity = '0';
      replayBtn.style.transition = 'opacity 200ms ease';
      bubble.appendChild(replayBtn);
    }
    messagesEl.appendChild(bubble);

    activeText = textEl;
    activeFinalText = response.text;
    activeChipsAfter = response.chips || null;

    let i = 0;
    function tick() {
      if (i >= response.text.length) {
        typewriterTimer = null;
        activeText = null;
        activeFinalText = null;
        if (replayBtn) replayBtn.style.opacity = '1';
        if (allowAutoplay && response.audio) playChatAudio(response.audio, replayBtn);
        if (activeChipsAfter) {
          setActiveChips(activeChipsAfter);
          activeChipsAfter = null;
        }
        setBusy(false);
        return;
      }
      textEl.textContent += response.text.charAt(i++);
      scrollToBottom();
      typewriterTimer = setTimeout(tick, 30);
    }
    typewriterTimer = setTimeout(tick, 30);
    scrollToBottom();
  }

  function setBusy(busy) {
    isProcessing = busy;
    if (input) input.disabled = busy;
    if (send) send.disabled = busy;
  }

  function showTypingIndicator() {
    const el = document.createElement('div');
    el.className = 'chat-typing';
    el.id = 'chat-typing';
    el.innerHTML = '<span></span><span></span><span></span>';
    messagesEl.appendChild(el);
    scrollToBottom();
  }
  function hideTypingIndicator() {
    const el = document.getElementById('chat-typing');
    if (el) el.remove();
  }

  function showAutoplayHint() {
    if (autoplayHintShown) return;
    autoplayHintShown = true;
    const el = document.createElement('div');
    el.className = 'chat-autoplay-hint';
    el.textContent = 'Tap a message ▶ to hear it — your browser blocks audio until you interact.';
    messagesEl.appendChild(el);
    scrollToBottom();
  }

  function playChatAudio(file, replayBtn) {
    if (!player || !file) return;
    try { player.pause(); player.currentTime = 0; } catch (e) {}

    // Clear any prior playing-state indicator.
    document.querySelectorAll('.chat-msg-replay[data-playing="true"]')
      .forEach((b) => b.removeAttribute('data-playing'));
    if (replayBtn) replayBtn.setAttribute('data-playing', 'true');

    player.src = file;
    const onEnd = () => {
      if (replayBtn) replayBtn.removeAttribute('data-playing');
      player.removeEventListener('ended', onEnd);
      player.removeEventListener('pause', onEnd);
    };
    player.addEventListener('ended', onEnd);
    player.addEventListener('pause', onEnd);

    const promise = player.play();
    if (promise && typeof promise.catch === 'function') {
      promise.catch(() => {
        // Browser blocked autoplay (most common on first page load).
        if (replayBtn) replayBtn.removeAttribute('data-playing');
        if (firstUserInteraction) showAutoplayHint();
      });
    }
  }

  function sendUserMessage(text) {
    text = (text || '').trim();
    if (!text) return;
    if (isProcessing) return;       // drop submits while a turn is in flight
    setBusy(true);
    firstUserInteraction = false;
    appendUserMessage(text);
    showTypingIndicator();
    const delay = 1100 + Math.floor(Math.random() * 800);  // 1.1-1.9s
    setTimeout(() => {
      hideTypingIndicator();
      const response = routeMessage(text);
      // appendKwameMessage will release the busy lock after the
      // typewriter completes (or finalisePendingTypewriter does it
      // early if a new message starts).
      appendKwameMessage(response);
    }, delay);
  }

  // Wire form
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    sendUserMessage(input.value);
    input.value = '';
    if (!isProcessing) input.focus();
  });

  // Wire replay buttons (event delegation)
  messagesEl.addEventListener('click', (e) => {
    const target = e.target.closest('.chat-msg-replay');
    if (!target) return;
    firstUserInteraction = false;
    playChatAudio(target.dataset.audio, target);
  });

  // Install initial chips (replaces any server-rendered placeholders
  // and binds proper click handlers).
  setActiveChips(INITIAL_CHIPS);

  // Initial greeting after 1 second. Audio autoplay may be blocked;
  // the showAutoplayHint() path handles that case. The greeting
  // intentionally does NOT engage the busy lock — the user should be
  // able to type a chip selection while Kwame's opener is still typing.
  setTimeout(() => {
    appendKwameMessage(Object.assign({}, GREETING, { chips: INITIAL_CHIPS }));
  }, 1000);
})();

// -- polish layer -----------------------------------------------------
//
// Scroll progress bar, back-to-top button, hero parallax, and a soft
// cursor-glow that follows the mouse on desktop. Every handler is
// gated on prefers-reduced-motion (reduce) returning false; reduced-
// motion users get the static page with no parallax and instant
// scroll restore.

(function() {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // -- shared scroll state, updated once per RAF ----------------------
  const progressEl = document.getElementById('cynea-scroll-progress');
  const backTopEl  = document.getElementById('cynea-back-to-top');
  const heroBg     = document.querySelector('.hero-cinematic .hero-bg');

  let lastScrollY = 0;
  let scrollScheduled = false;

  function readScroll() {
    scrollScheduled = false;
    const y = window.scrollY || window.pageYOffset || 0;
    const docH = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    const pct = Math.max(0, Math.min(100, (y / docH) * 100));

    if (progressEl) {
      progressEl.style.width = pct.toFixed(2) + '%';
    }

    if (backTopEl) {
      const shouldShow = y > 500;
      if (shouldShow && backTopEl.hidden) {
        backTopEl.hidden = false;
        // Force reflow before adding the class so the transition runs.
        void backTopEl.offsetWidth;
        backTopEl.classList.add('visible');
      } else if (!shouldShow && !backTopEl.hidden) {
        backTopEl.classList.remove('visible');
        // Hide after the transition so :focus traps don't leak.
        setTimeout(() => { if ((window.scrollY || 0) <= 500) backTopEl.hidden = true; }, 250);
      }
    }

    if (heroBg && !reduceMotion) {
      // Background drifts at 30% of scroll speed — subtle parallax.
      const offset = Math.max(-200, -y * 0.3);
      heroBg.style.setProperty('--parallax-y', offset.toFixed(1) + 'px');
    }

    lastScrollY = y;
  }

  function onScroll() {
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(readScroll);
  }

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll, { passive: true });
  readScroll();  // initial paint

  // -- back-to-top click ----------------------------------------------
  if (backTopEl) {
    backTopEl.addEventListener('click', () => {
      window.scrollTo({
        top: 0,
        behavior: reduceMotion ? 'auto' : 'smooth',
      });
    });
  }

  // -- cursor glow (desktop only — CSS handles the visibility gate) ---
  const glowEl = document.getElementById('cynea-cursor-glow');
  if (glowEl && !reduceMotion) {
    let glowX = window.innerWidth / 2;
    let glowY = window.innerHeight / 2;
    let glowScheduled = false;

    function paintGlow() {
      glowScheduled = false;
      glowEl.style.transform =
        'translate3d(' + glowX.toFixed(0) + 'px, ' + glowY.toFixed(0) + 'px, 0)';
    }

    window.addEventListener('mousemove', (e) => {
      glowX = e.clientX;
      glowY = e.clientY;
      glowEl.classList.add('active');
      if (glowScheduled) return;
      glowScheduled = true;
      requestAnimationFrame(paintGlow);
    }, { passive: true });

    window.addEventListener('mouseleave', () => {
      glowEl.classList.remove('active');
    });
  }
})();

// -- nav active link highlight on scroll ------------------------------
(function() {
  const ids = ['agents', 'timeline', 'features', 'dashboard'];
  const sections = ids.map(id => document.getElementById(id)).filter(Boolean);
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
