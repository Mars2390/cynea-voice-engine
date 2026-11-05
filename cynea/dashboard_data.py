"""Cynea Voice Engine — dashboard read models.

Turns database rows into the exact shapes `dashboard.html` renders, so the
front end never has to reshape or recompute anything. Every function here
is read-only.

Aggregation happens in SQL, not Python: a workspace with 100k calls should
not stream them all into the process to count today's. The queries below
group in the database and return a handful of rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import func, select

from cynea import db

log = logging.getLogger("cynea.dashboard")

# Agent display metadata. The database stores which persona an agent uses;
# these are the presentation details the console shows beside it.
# `name` is what the agent is called on screen, which is not always the
# persona key: the "kwame" persona is presented as Peter. Keys stay as they
# are because they are stored in agents.persona and in asset file names.
# No location labels: the product is positioned for every market.
PERSONA_META = {
    "kwame": {"name": "Peter", "role": "Hotel Receptionist",    "photo": "avatar-kwame"},
    "amina": {"name": "Amina", "role": "Bank Support",          "photo": "avatar-amina"},
    "kofi":  {"name": "Kofi",  "role": "Restaurant Orders",     "photo": "avatar-kofi"},
    "maya":  {"name": "Maya",  "role": "Bookings & Scheduling", "photo": "avatar-maya"},
}


def _display_name(persona: str) -> str:
    return PERSONA_META.get(persona, {}).get("name") or persona.title()

_STATUS_TO_BADGE = {"resolved": "ok", "escalated": "warn", "abandoned": "bad"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes for timezone=True columns."""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """An instant the browser can show in the viewer's own time."""
    return _aware(dt).isoformat() if dt else None


def _start_of_today(tz_offset_min: int = 0) -> datetime:
    """Midnight where the viewer is, as a UTC instant."""
    shift = timedelta(minutes=tz_offset_min)
    local = _now() + shift
    return local.replace(hour=0, minute=0, second=0, microsecond=0) - shift


def agent_voice(agent) -> dict:
    """The voice a call on this agent will use.

    Rows saved before every agent moved to Edge can still name a provider that
    nothing runs (Kofi's ElevenLabs config). Those get the persona's own voice,
    so the console shows what a caller would actually hear.
    """
    from cynea.agent_loader import _PERSONAS

    saved = agent.voice_config or {}
    if saved.get("provider", "edge_tts") == "edge_tts" and saved.get("voice"):
        return {"provider": "edge_tts", "voice": saved["voice"],
                "speed": float(saved.get("speed", 1.0))}
    spec = (_PERSONAS.get(agent.persona) or {}).get("voice") or {}
    return {"provider": "edge_tts", "voice": spec.get("voice", "en-US-AriaNeural"),
            "speed": float(spec.get("speed", 1.0))}


def _mmss(seconds: Optional[int]) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def _mask_number(number: str) -> str:
    """Hide all but the last four digits.

    Console users need to tell two callers apart, not dial them. A short value
    is not a phone number worth masking ("browser").
    """
    if not number or len(number) < 7:
        return number or "unknown"
    return f"••• {number[-4:]}"


def _sentiment_label(score: Optional[float]) -> str:
    if score is None:
        return "not scored"
    sign = "+" if score > 0 else ""
    if score >= 0.25:
        word = "positive"
    elif score <= -0.25:
        word = "negative"
    else:
        word = "neutral"
    return f"{sign}{score:.2f} {word}"


# ----------------------------------------------------------------------
# Call history
# ----------------------------------------------------------------------

def get_call_history(agent_id: Optional[str] = None, limit: int = 50,
                     user_id: Optional[str] = None) -> List[dict]:
    """Recent calls, newest first, shaped for the history table.

    Pass `agent_id` for one agent, or `user_id` for every agent in a
    workspace. With neither, returns the most recent calls overall.
    """
    with db.session_scope() as s:
        stmt = (
            select(db.Call, db.Agent)
            .join(db.Agent, db.Call.agent_id == db.Agent.id)
            .order_by(db.Call.created_at.desc())
            .limit(min(int(limit), 500))
        )
        if agent_id:
            stmt = stmt.where(db.Call.agent_id == str(agent_id))
        if user_id:
            stmt = stmt.where(db.Agent.user_id == str(user_id))

        rows = s.execute(stmt).all()

        out = []
        for call, agent in rows:
            meta = PERSONA_META.get(agent.persona, {})
            created = call.created_at
            out.append({
                "id": call.id,
                "created_at": _iso(created),
                # UTC. The console shows created_at in the viewer's own time.
                "time": created.strftime("%H:%M") if created else "--:--",
                "date": created.strftime("%Y-%m-%d") if created else None,
                "agent": agent.name,
                "agent_id": agent.id,
                "persona": agent.persona,
                "photo": meta.get("photo", "avatar-kwame"),
                "caller": _mask_number(call.caller_number),
                "duration": _mmss(call.duration_s),
                "duration_s": call.duration_s,
                # First transcript line is the outcome summary the engine
                # writes; fall back to the status when there is none.
                "outcome": (call.transcript or "").splitlines()[0].strip()
                           if call.transcript else call.status.title(),
                "status": _STATUS_TO_BADGE.get(call.status, "bad"),
                "status_raw": call.status,
                "sentiment": _sentiment_label(call.sentiment_score),
                "sentiment_score": call.sentiment_score,
                "cost": f"${call.cost_cents / 100:.2f}",
                "cost_cents": call.cost_cents,
            })
        return out


def get_call_detail(call_id: str) -> Optional[dict]:
    """One call with its full transcript, shaped for the detail modal."""
    with db.session_scope() as s:
        row = s.execute(
            select(db.Call, db.Agent)
            .join(db.Agent, db.Call.agent_id == db.Agent.id)
            .where(db.Call.id == str(call_id))
        ).first()
        if row is None:
            return None
        call, agent = row

    lines = (call.transcript or "").splitlines()
    summary = lines[0].strip() if lines else ""
    body = [ln for ln in lines[1:] if ln.strip()]

    turns = []
    for line in body:
        speaker, _, text = line.partition(":")
        if not text:
            continue
        is_caller = speaker.strip().lower() == "caller"
        turns.append({
            "speaker": "caller" if is_caller else "agent",
            "label": "Caller" if is_caller else f"{agent.name} · agent",
            "text": text.strip(),
        })

    meta = PERSONA_META.get(agent.persona, {})
    return {
        "id": call.id,
        "agent": agent.name,
        "agent_id": agent.id,
        "persona": agent.persona,
        "photo": meta.get("photo", "avatar-kwame"),
        "created_at": _iso(call.created_at),
        "time": call.created_at.strftime("%H:%M") if call.created_at else "--:--",
        "date": call.created_at.strftime("%Y-%m-%d") if call.created_at else None,
        "caller": _mask_number(call.caller_number),
        "duration": _mmss(call.duration_s),
        "status": _STATUS_TO_BADGE.get(call.status, "bad"),
        "status_raw": call.status,
        "status_label": call.status.title(),
        "sentiment": _sentiment_label(call.sentiment_score),
        "cost": f"${call.cost_cents / 100:.2f}",
        "summary": summary or "No summary recorded for this call.",
        "turns": turns,
        "transcript": call.transcript or "",
    }


# ----------------------------------------------------------------------
# Aggregates
# ----------------------------------------------------------------------

def get_dashboard_stats(user_id: Optional[str] = None, tz_offset_min: int = 0) -> dict:
    """KPI row, charts, and agent status.

    `tz_offset_min` is the viewer's offset from UTC in minutes, so "today" and
    the hour labels mean what they mean to the person reading them.
    """
    today = _start_of_today(tz_offset_min)
    week_ago = _now() - timedelta(days=7)
    shift = timedelta(minutes=tz_offset_min)

    with db.session_scope() as s:
        def scoped(stmt):
            if user_id:
                stmt = stmt.join(db.Agent, db.Call.agent_id == db.Agent.id) \
                           .where(db.Agent.user_id == str(user_id))
            return stmt

        # --- today -----------------------------------------------------
        today_rows = s.execute(scoped(
            select(db.Call.status, func.count(db.Call.id),
                   func.coalesce(func.sum(db.Call.cost_cents), 0))
            .where(db.Call.created_at >= today)
            .group_by(db.Call.status)
        )).all()

        calls_today = sum(r[1] for r in today_rows)
        cost_today = sum(r[2] for r in today_rows)
        resolved_today = sum(r[1] for r in today_rows if r[0] == "resolved")

        # Over the calls themselves. Averaging a per-status average would give
        # one escalated call the same weight as a hundred resolved ones.
        avg_sentiment = s.scalar(scoped(
            select(func.avg(db.Call.sentiment_score)).where(db.Call.created_at >= today)
        ))

        # --- last 7 days, for the delta comparisons --------------------
        week_total, week_resolved, week_cost = s.execute(scoped(
            select(func.count(db.Call.id),
                   func.count(db.Call.id).filter(db.Call.status == "resolved"),
                   func.coalesce(func.sum(db.Call.cost_cents), 0))
            .where(db.Call.created_at >= week_ago)
        )).one()

        # --- hourly volume, today --------------------------------------
        # Bucketed here, in the viewer's hours. One day of rows, and SQL would
        # need a different expression per dialect for a fractional offset.
        buckets: dict = {}
        for created, score in s.execute(scoped(
            select(db.Call.created_at, db.Call.sentiment_score)
            .where(db.Call.created_at >= today)
        )):
            bucket = buckets.setdefault((_aware(created) + shift).hour, [0, []])
            bucket[0] += 1
            if score is not None:
                bucket[1].append(score)

        # --- cost split by agent ---------------------------------------
        by_agent = s.execute(
            select(db.Agent.id, db.Agent.name,
                   func.count(db.Call.id),
                   func.coalesce(func.sum(db.Call.cost_cents), 0))
            .join(db.Call, db.Call.agent_id == db.Agent.id)
            .where(db.Call.created_at >= week_ago,
                   *( [db.Agent.user_id == str(user_id)] if user_id else [] ))
            .group_by(db.Agent.id, db.Agent.name)
            .order_by(func.sum(db.Call.cost_cents).desc())
        ).all()

        # --- agent roster ----------------------------------------------
        agent_stmt = select(db.Agent)
        if user_id:
            agent_stmt = agent_stmt.where(db.Agent.user_id == str(user_id))
        agents = list(s.scalars(agent_stmt.order_by(db.Agent.created_at)))
        agent_cards = []
        for a in agents:
            meta = PERSONA_META.get(a.persona, {})
            n_today = s.scalar(
                select(func.count(db.Call.id))
                .where(db.Call.agent_id == a.id, db.Call.created_at >= today)
            ) or 0
            agent_cards.append({
                "id": a.id,
                "name": a.name,
                "persona_name": _display_name(a.persona),
                "role": meta.get("role", "Voice agent"),
                "photo": meta.get("photo", "avatar-kwame"),
                "persona": a.persona,
                "calls_today": n_today,
                "voice": agent_voice(a)["voice"],
                "paused": bool((a.voice_config or {}).get("paused")),
            })

    containment = round(resolved_today / calls_today * 100) if calls_today else 0
    week_containment = round(week_resolved / week_total * 100) if week_total else 0
    cost_per_call = (cost_today / calls_today / 100) if calls_today else 0.0

    volume = [{"h": f"{h:02d}", "v": n} for h, (n, _) in sorted(buckets.items())]
    sentiment_series = [
        {"h": f"{h:02d}", "v": round(sum(scores) / len(scores), 3)}
        for h, (_, scores) in sorted(buckets.items()) if scores
    ]

    return {
        "generated_at": _now().isoformat(),
        "source": "database",
        "kpis": {
            "calls_today": calls_today,
            "containment_pct": containment,
            "cost_per_call": round(cost_per_call, 2),
            "cost_today_cents": int(cost_today),
            "cost_week_cents": int(week_cost),
            "avg_sentiment": round(float(avg_sentiment), 3) if avg_sentiment is not None else None,
            "containment_delta_pts": containment - week_containment,
        },
        "volume": volume,
        "sentiment": sentiment_series,
        "cost_breakdown": [
            {"k": name, "agent_id": agent_id, "v": round(cents / 100, 2), "calls": int(n)}
            for agent_id, name, n, cents in by_agent
        ],
        "agents": agent_cards,
        "totals": {"week_calls": int(week_total), "week_containment_pct": week_containment},
    }


def get_live_queue(user_id: Optional[str] = None) -> List[dict]:
    """Calls logged as abandoned in the last 15 minutes.

    Nothing here is known to be live. A row is "abandoned" until end_call()
    resolves it, so this is either a call in progress or one that was dropped,
    and the row cannot say which. It is labelled as what it is, and its age is
    how long ago it started, not how long anybody has been waiting.
    """
    cutoff = _now() - timedelta(minutes=15)
    with db.session_scope() as s:
        stmt = (
            select(db.Call, db.Agent)
            .join(db.Agent, db.Call.agent_id == db.Agent.id)
            .where(db.Call.status == "abandoned", db.Call.created_at >= cutoff)
            .order_by(db.Call.created_at.desc())
        )
        if user_id:
            stmt = stmt.where(db.Agent.user_id == str(user_id))
        rows = s.execute(stmt).all()

    out = []
    for call, agent in rows:
        age = int((_now() - _aware(call.created_at)).total_seconds())
        out.append({
            "id": call.id,
            "num": _mask_number(call.caller_number),
            "cat": ((call.transcript or "").splitlines() or ["Not finished"])[0][:40],
            "age": _mmss(age),
            "age_s": age,
            "created_at": _iso(call.created_at),
            "agent": agent.name,
        })
    return out


__all__ = [
    "get_call_history", "get_call_detail", "get_dashboard_stats",
    "get_live_queue", "PERSONA_META",
]
