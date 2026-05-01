"""Cynea Africa — operations dashboard generator.

Reads `examples/_out/calls.json` (the file produced by
`MetricsTracker.export_json()`) and renders a self-contained HTML
dashboard that an AE or CSM can show to a hotel owner, bank manager,
or restaurant chain.

Design language matches `cynea_africa/theme.py`:
    background  #050505
    cards       #111111   border #1E1E1E
    accent      #00D4FF   (cyan — Cynea signature)
    text        #F5F5F5   muted #8A8A8A
    success     #10B981
    warning     #F59E0B
    error       #EF4444

Fonts: Syne (headings), DM Sans (body), JetBrains Mono (numbers).
We pull these from Google Fonts via a single <link>; everything else
in the file is inline so the page works without a CDN for assets.

Usage:
    from cynea_africa.dashboard.preview import generate_dashboard
    path = generate_dashboard()
    print(f"Open {path} in your browser.")

Or from CLI:
    python -m cynea_africa.dashboard.preview
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import sys
from typing import Optional


# ---------------------------------------------------------------------
# Cynea brand tokens (kept inline so this module is self-contained even
# if cynea_africa.theme is missing).
# ---------------------------------------------------------------------

_COLORS = {
    "bg": "#050505",
    "bg2": "#0E0E0E",
    "card": "#111111",
    "border": "#1E1E1E",
    "text": "#F5F5F5",
    "muted": "#8A8A8A",
    "dim": "#777777",
    "accent": "#00D4FF",
    "accent_dark": "#0088AA",
    "green": "#10B981",
    "red": "#EF4444",
    "amber": "#F59E0B",
    "purple": "#A78BFA",
}


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def generate_dashboard(
    metrics_file: str = "examples/_out/calls.json",
    output_file: Optional[str] = None,
    client_name: str = "Adinkra Hotel",
    agent_display_name: Optional[str] = None,
) -> str:
    """Read a calls.json file and write a self-contained dashboard HTML.

    Args:
        metrics_file: Path to the JSON produced by
            `MetricsTracker.export_json()`. Resolved relative to CWD.
        output_file: Where to write the HTML. Defaults to a sibling
            `dashboard.html` next to the input file (or
            `examples/_out/dashboard.html` if the input is missing).
        client_name: Friendly name for the customer (header subtitle).
        agent_display_name: Friendly name for the agent. Falls back to
            the agent string in the first call record.

    Returns:
        The absolute path of the file we wrote.
    """
    payload = _load_metrics(metrics_file)
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


# ---------------------------------------------------------------------
# Formatting helpers (named per the spec)
# ---------------------------------------------------------------------

def _format_cost(cents: Optional[float]) -> str:
    """Render a cost value in cents as 'X.X¢' (one decimal). None → '—'."""
    if cents is None:
        return "—"
    try:
        value = float(cents)
    except (TypeError, ValueError):
        return "—"
    return f"{value:.1f}¢"


def _sentiment_badge(score: Optional[float]) -> str:
    """Return an HTML <span> badge for a sentiment score in [-1, 1]."""
    if score is None:
        return _badge("—", _COLORS["muted"], _COLORS["dim"])
    try:
        s = float(score)
    except (TypeError, ValueError):
        return _badge("—", _COLORS["muted"], _COLORS["dim"])

    if s >= 0.2:
        emoji, fg, dot = "Positive", _COLORS["green"], _COLORS["green"]
    elif s <= -0.2:
        emoji, fg, dot = "Negative", _COLORS["red"], _COLORS["red"]
    else:
        emoji, fg, dot = "Neutral", _COLORS["muted"], _COLORS["dim"]
    label = f"{emoji} · {s:+.2f}"
    return _badge(label, fg, dot)


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------

def _load_metrics(metrics_file: str) -> dict:
    """Return a payload with `summary`, `calls`, and a couple of marker
    keys (`_is_sample_data`, `_is_empty_state`) the renderer uses to
    pick a tone for the page.

    Behavior:
      - File missing or unreadable → sample data (still impressive).
      - File present but contains zero calls → empty state.
      - File present with calls → real data.
    """
    try:
        with open(metrics_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _sample_payload()

    if isinstance(data, list):
        # Tolerate a bare list of calls (older export shape).
        data = {"calls": data, "summary": {}}

    if not isinstance(data, dict):
        return _sample_payload()

    calls = data.get("calls") or []
    if not calls:
        data["_is_empty_state"] = True
    return data


def _derive_agent_display_name(calls: list) -> str:
    if not calls:
        return "Kwame"
    raw = (calls[0].get("agent") or "Kwame").split("_")[0]
    return raw.capitalize()


def _badge(label: str, fg: str, dot_color: str) -> str:
    return (
        f'<span class="badge" style="color:{fg};border-color:{fg}33;background:{fg}14">'
        f'<span class="dot" style="background:{dot_color}"></span>'
        f'{html.escape(label)}'
        f'</span>'
    )


def _short_id(call_id: str) -> str:
    return call_id[:8] if isinstance(call_id, str) and len(call_id) > 8 else (call_id or "—")


def _format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v < 60:
        return f"{v:.1f}s"
    minutes, seconds = divmod(v, 60)
    return f"{int(minutes)}m {int(seconds):02d}s"


def _format_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
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
    d1 = _dt.datetime.fromtimestamp(earliest).strftime("%b %d, %Y")
    d2 = _dt.datetime.fromtimestamp(latest).strftime("%b %d, %Y")
    return d1 if d1 == d2 else f"{d1} → {d2}"


def _containment_color(rate: Optional[float]) -> str:
    if rate is None:
        return _COLORS["muted"]
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return _COLORS["muted"]
    if r >= 0.8:
        return _COLORS["green"]
    if r >= 0.6:
        return _COLORS["amber"]
    return _COLORS["red"]


def _sentiment_emoji(score: Optional[float]) -> str:
    if score is None:
        return "—"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "—"
    if s >= 0.2:
        return "▲"
    if s <= -0.2:
        return "▼"
    return "■"


def _sample_payload() -> dict:
    """Realistic sample data — used when calls.json is missing.

    Numbers chosen so the dashboard tells a believable story for an
    Accra hotel running Kwame for one week.
    """
    now = _dt.datetime.now()
    base = now.timestamp() - 7 * 86400
    sample_calls = []
    for i in range(12):
        started = base + i * 13_500 + (i % 3) * 240
        duration = 35 + (i * 7) % 90
        user_turns = 4 + (i % 5)
        assistant_turns = user_turns + 1
        interruptions = (i % 4 == 0)
        sentiment = 0.45 - 0.08 * (i % 6)
        cost = 3.6 + 0.7 * (i % 5)
        sample_calls.append({
            "call_id": f"demo-{i:04d}-{['kim','abena','kojo','adwoa','yaa','kofi'][i % 6]}",
            "agent": "kwame_adinkra",
            "started_at": started,
            "ended_at": started + duration,
            "duration_s": duration,
            "user_turns": user_turns,
            "assistant_turns": assistant_turns,
            "interruptions": 1 if interruptions else 0,
            "sentiment_score": round(sentiment, 3),
            "containment": (i % 7) != 0,
            "resolution": (i % 9) != 0,
            "cost_total_cents": round(cost, 3),
            "cost_breakdown": {
                "stt_cents": 0.0,
                "llm_cents": round(cost * 0.18, 3),
                "tts_cents": 0.0,
                "telephony_cents": round(cost * 0.82, 3),
            },
        })
    summary = {
        "calls": len(sample_calls),
        "containment_rate": sum(1 for c in sample_calls if c["containment"]) / len(sample_calls),
        "resolution_rate": sum(1 for c in sample_calls if c["resolution"]) / len(sample_calls),
        "avg_duration_s": sum(c["duration_s"] for c in sample_calls) / len(sample_calls),
        "avg_cost_cents": sum(c["cost_total_cents"] for c in sample_calls) / len(sample_calls),
        "total_cost_cents": sum(c["cost_total_cents"] for c in sample_calls),
        "avg_sentiment": sum(c["sentiment_score"] for c in sample_calls) / len(sample_calls),
        "interruptions_per_call": sum(c["interruptions"] for c in sample_calls) / len(sample_calls),
    }
    return {"summary": summary, "calls": sample_calls, "_is_sample_data": True}


# ---------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------

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

    n_calls = summary.get("calls") or len(calls) or 0
    containment = summary.get("containment_rate")
    avg_cost = summary.get("avg_cost_cents")
    avg_sentiment = summary.get("avg_sentiment")
    avg_duration = summary.get("avg_duration_s")
    total_cost = summary.get("total_cost_cents")
    resolution = summary.get("resolution_rate")
    interruptions = summary.get("interruptions_per_call")

    cards_html = _render_cards(
        n_calls=n_calls,
        containment=containment,
        avg_cost=avg_cost,
        avg_sentiment=avg_sentiment,
        is_empty_state=is_empty_state,
    )
    table_html = _render_calls_table(calls, is_empty_state=is_empty_state)
    fleet_html = _render_fleet_summary(
        n_calls=n_calls,
        avg_duration=avg_duration,
        total_cost=total_cost,
        avg_sentiment=avg_sentiment,
        containment=containment,
        resolution=resolution,
        interruptions=interruptions,
        is_empty_state=is_empty_state,
    )

    banner = ""
    if is_sample_data:
        banner = (
            '<div class="banner">'
            'Showing sample data — drop a real <code>calls.json</code> at '
            '<code>examples/_out/</code> to render live metrics.'
            '</div>'
        )
    elif is_empty_state:
        banner = (
            '<div class="banner">'
            'No calls recorded yet. The dashboard will populate as soon '
            'as the agent handles its first call.'
            '</div>'
        )

    css = _render_css()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cynea Voice Engine — {html.escape(agent_display_name)} @ {html.escape(client_name)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<main>
  <header class="hero">
    <div class="brand">
      <div class="dot-accent"></div>
      <span class="brand-name">Cynea Voice Engine</span>
    </div>
    <h1>Operations Dashboard</h1>
    <p class="subtitle">{html.escape(agent_display_name)} <span class="sep">·</span> {html.escape(client_name)}</p>
    <p class="daterange">{html.escape(date_range)}</p>
  </header>

  {banner}

  <section class="cards">
    {cards_html}
  </section>

  <section class="panel">
    <header class="panel-head">
      <h2>Recent calls</h2>
      <span class="muted">{n_calls} total</span>
    </header>
    {table_html}
  </section>

  <section class="panel">
    <header class="panel-head">
      <h2>Fleet summary</h2>
      <span class="muted">All-time</span>
    </header>
    {fleet_html}
  </section>

  <footer>
    <div>
      <strong>Cynea AI</strong><span class="dim"> — Made in Kenya</span>
    </div>
    <div class="dim">Generated {html.escape(generated_at)}</div>
  </footer>
</main>
</body>
</html>
"""


def _render_cards(
    *,
    n_calls: int,
    containment: Optional[float],
    avg_cost: Optional[float],
    avg_sentiment: Optional[float],
    is_empty_state: bool,
) -> str:
    if is_empty_state:
        # Show structure but with dashes — no false confidence on a fresh deploy.
        return "".join([
            _card("Total calls", "0", "Since deployment", _COLORS["accent"]),
            _card("Containment rate", "—", ">80% target", _COLORS["muted"]),
            _card("Avg cost / call", "—", "Per-provider breakdown below", _COLORS["muted"]),
            _card("Avg sentiment", "—", "Range −1 to +1", _COLORS["muted"]),
        ])

    contain_pct = _format_pct(containment)
    contain_color = _containment_color(containment)
    cost_str = _format_cost(avg_cost)
    sent_str = "—" if avg_sentiment is None else f"{float(avg_sentiment):+.2f}"
    sent_emoji = _sentiment_emoji(avg_sentiment)
    sent_color = _COLORS["green"] if (avg_sentiment or 0) >= 0.2 else (
        _COLORS["red"] if (avg_sentiment or 0) <= -0.2 else _COLORS["muted"]
    )

    return "".join([
        _card("Total calls", f"{n_calls:,}", "Since deployment", _COLORS["accent"]),
        _card("Containment rate", contain_pct, ">80% target", contain_color),
        _card("Avg cost / call", cost_str, "Per-provider breakdown in CSV", _COLORS["accent"]),
        _card("Avg sentiment", f"{sent_emoji} {sent_str}", "Range −1 to +1", sent_color),
    ])


def _card(label: str, value: str, subtitle: str, value_color: str) -> str:
    return (
        '<article class="card">'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value" style="color:{value_color}">{html.escape(value)}</div>'
        f'<div class="sub">{html.escape(subtitle)}</div>'
        '</article>'
    )


def _render_calls_table(calls: list, *, is_empty_state: bool) -> str:
    if is_empty_state or not calls:
        return (
            '<div class="empty">No calls yet — '
            'the table will populate after the first conversation.</div>'
        )

    rows = []
    # Show most-recent first; cap to 20 rows so the page stays printable.
    for c in sorted(calls, key=lambda x: x.get("started_at") or 0, reverse=True)[:20]:
        rows.append(_render_call_row(c))

    return (
        '<div class="table-wrap"><table>'
        '<thead><tr>'
        '<th>Call ID</th>'
        '<th class="num">Duration</th>'
        '<th class="num">User</th>'
        '<th class="num">Agent</th>'
        '<th class="num">Interrupt</th>'
        '<th>Sentiment</th>'
        '<th class="num">Cost</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table></div>'
    )


def _render_call_row(c: dict) -> str:
    return (
        '<tr>'
        f'<td class="mono">{html.escape(_short_id(c.get("call_id", "")))}</td>'
        f'<td class="num">{html.escape(_format_seconds(c.get("duration_s")))}</td>'
        f'<td class="num">{int(c.get("user_turns") or 0)}</td>'
        f'<td class="num">{int(c.get("assistant_turns") or 0)}</td>'
        f'<td class="num">{int(c.get("interruptions") or 0)}</td>'
        f'<td>{_sentiment_badge(c.get("sentiment_score"))}</td>'
        f'<td class="num">{html.escape(_format_cost(c.get("cost_total_cents")))}</td>'
        '</tr>'
    )


def _render_fleet_summary(
    *,
    n_calls: int,
    avg_duration: Optional[float],
    total_cost: Optional[float],
    avg_sentiment: Optional[float],
    containment: Optional[float],
    resolution: Optional[float],
    interruptions: Optional[float],
    is_empty_state: bool,
) -> str:
    rows = [
        ("Calls handled", f"{n_calls:,}" if not is_empty_state else "0"),
        ("Avg duration", _format_seconds(avg_duration) if not is_empty_state else "—"),
        ("Total cost", _format_cost(total_cost) if not is_empty_state else "—"),
        ("Avg sentiment", f"{float(avg_sentiment):+.2f}" if (avg_sentiment is not None and not is_empty_state) else "—"),
        ("Containment rate", _format_pct(containment) if not is_empty_state else "—"),
        ("Resolution rate", _format_pct(resolution) if not is_empty_state else "—"),
        ("Interruptions / call", f"{float(interruptions):.2f}" if (interruptions is not None and not is_empty_state) else "—"),
    ]
    items = "".join(
        f'<li><span class="muted">{html.escape(label)}</span>'
        f'<span class="mono value-sm">{html.escape(value)}</span></li>'
        for label, value in rows
    )
    return f'<ul class="fleet">{items}</ul>'


# ---------------------------------------------------------------------
# CSS — kept here, inline, deliberately readable
# ---------------------------------------------------------------------

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
}}
main {{ max-width: 1180px; margin: 0 auto; padding: 56px 32px 80px; }}

.hero {{ margin-bottom: 28px; }}
.brand {{ display: flex; align-items: center; gap: 10px; margin-bottom: 22px; }}
.brand-name {{
  font-family: 'Syne', sans-serif; font-weight: 700; letter-spacing: 0.02em;
  font-size: 14px; color: {c['accent']}; text-transform: uppercase;
}}
.dot-accent {{
  width: 10px; height: 10px; border-radius: 50%;
  background: {c['accent']};
  box-shadow: 0 0 12px {c['accent']}88;
}}
h1 {{
  font-family: 'Syne', sans-serif; font-weight: 800;
  font-size: 44px; line-height: 1.1; margin: 0 0 8px;
  letter-spacing: -0.01em;
}}
.subtitle {{
  font-size: 18px; color: {c['text']}; margin: 0 0 4px;
  font-weight: 500;
}}
.subtitle .sep {{ color: {c['dim']}; margin: 0 8px; }}
.daterange {{
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; color: {c['muted']}; margin: 0;
}}

.banner {{
  margin: 16px 0 24px; padding: 12px 16px;
  border: 1px solid {c['accent']}33; background: {c['accent']}10;
  color: {c['accent']}; border-radius: 10px;
  font-size: 13px;
}}
.banner code {{
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  background: {c['bg2']}; padding: 1px 6px; border-radius: 4px;
}}

/* metric cards */
.cards {{
  display: grid; gap: 16px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 24px 0 40px;
}}
@media (max-width: 900px) {{ .cards {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 520px) {{ .cards {{ grid-template-columns: 1fr; }} }}

.card {{
  background: {c['card']}; border: 1px solid {c['border']};
  border-radius: 14px; padding: 22px 22px 20px;
  position: relative; overflow: hidden;
}}
.card::before {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, {c['accent']}06, transparent 40%);
}}
.card .label {{
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
  color: {c['muted']}; font-weight: 600;
}}
.card .value {{
  font-family: 'JetBrains Mono', monospace; font-weight: 500;
  font-size: 32px; margin: 10px 0 6px;
  font-feature-settings: "tnum" 1; letter-spacing: -0.01em;
}}
.card .sub {{ font-size: 12px; color: {c['dim']}; }}

/* panels */
.panel {{
  background: {c['card']}; border: 1px solid {c['border']};
  border-radius: 14px; padding: 0; margin-bottom: 28px;
}}
.panel-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  padding: 18px 22px 12px; border-bottom: 1px solid {c['border']};
}}
.panel h2 {{
  font-family: 'Syne', sans-serif; font-weight: 700;
  font-size: 16px; margin: 0; letter-spacing: 0.02em;
  text-transform: uppercase;
}}
.muted {{ color: {c['muted']}; font-size: 13px; }}
.dim {{ color: {c['dim']}; }}

/* table */
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; }}
thead th {{
  text-align: left; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.08em; color: {c['muted']}; font-weight: 600;
  padding: 14px 22px; border-bottom: 1px solid {c['border']};
  background: {c['bg2']};
}}
thead th.num {{ text-align: right; }}
tbody td {{
  padding: 12px 22px; font-size: 13px;
  border-bottom: 1px solid {c['border']};
  vertical-align: middle;
}}
tbody tr:nth-child(odd) td {{ background: rgba(255,255,255,0.012); }}
tbody tr:hover td {{ background: {c['accent']}08; }}
tbody tr:last-child td {{ border-bottom: none; }}
.num {{
  text-align: right; font-family: 'JetBrains Mono', monospace;
  font-feature-settings: "tnum" 1;
}}
.mono {{
  font-family: 'JetBrains Mono', monospace;
  font-feature-settings: "tnum" 1;
  font-size: 12px;
}}

.empty {{
  padding: 44px 22px; text-align: center; color: {c['muted']};
  font-size: 14px;
}}

/* fleet summary */
.fleet {{
  list-style: none; margin: 0; padding: 8px 22px 18px;
  display: grid; gap: 0;
  grid-template-columns: 1fr 1fr;
}}
@media (max-width: 720px) {{ .fleet {{ grid-template-columns: 1fr; }} }}
.fleet li {{
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 14px 0; border-bottom: 1px solid {c['border']};
}}
.fleet li:nth-last-child(-n+1):nth-child(odd) {{ border-bottom: none; }}
.fleet li:last-child {{ border-bottom: none; }}
.value-sm {{ font-size: 14px; color: {c['text']}; }}

/* sentiment badge */
.badge {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 500;
  border: 1px solid; line-height: 1;
  font-family: 'DM Sans', sans-serif;
}}
.badge .dot {{
  width: 6px; height: 6px; border-radius: 50%;
  display: inline-block;
}}

footer {{
  display: flex; justify-content: space-between; align-items: center;
  margin-top: 56px; padding-top: 22px;
  border-top: 1px solid {c['border']};
  font-size: 12px;
}}
"""


# ---------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------

def _main(argv: list) -> int:
    metrics_file = "examples/_out/calls.json"
    if len(argv) > 1:
        metrics_file = argv[1]
    try:
        path = generate_dashboard(metrics_file=metrics_file)
    except Exception as exc:  # never crash the operator's terminal
        print(f"[preview] failed to generate dashboard: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
