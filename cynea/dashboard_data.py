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
PERSONA_META = {
    "kwame": {"role": "Hotel Receptionist",   "place": "Ghana",       "photo": "avatar-kwame"},
    "amina": {"role": "Bank Support",          "place": "Kenya",       "photo": "avatar-amina"},
    "kofi":  {"role": "Restaurant Orders",     "place": "Ghana",       "photo": "avatar-kofi"},
    "maya":  {"role": "Bookings & Scheduling", "place": "Pan-African", "photo": "avatar-maya"},
}

_STATUS_TO_BADGE = {"resolved": "ok", "escalated": "warn", "abandoned": "bad"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_today() -> datetime:
    n = _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _mmss(seconds: Optional[int]) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def _mask_number(number: str) -> str:
    """Hide the subscriber digits.

    Console users are supervisors, not account holders — they need to tell
    two callers apart, not dial them. Storing the full number and masking
    on read keeps callback possible through a permissioned path later.
    """
    if not number or len(number) < 7:
        return number or "unknown"
    return f"{number[:7]} ••• {number[-4:]}"


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
                "time": created.strftime("%H:%M") if created else "--:--",
                "date": created.strftime("%Y-%m-%d") if created else None,
                "agent": agent.persona.title(),
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
            "label": "Caller" if is_caller else f"{agent.persona.title()} · agent",
            "text": text.strip(),
        })

    meta = PERSONA_META.get(agent.persona, {})
    return {
        "id": call.id,
        "agent": agent.persona.title(),
        "persona": agent.persona,
        "photo": meta.get("photo", "avatar-kwame"),
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

def get_dashboard_stats(user_id: Optional[str] = None) -> dict:
    """KPI row, charts, and agent status — all computed in SQL."""
    today = _start_of_today()
    week_ago = _now() - timedelta(days=7)

    with db.session_scope() as s:
        def scoped(stmt):
            if user_id:
                stmt = stmt.join(db.Agent, db.Call.agent_id == db.Agent.id) \
                           .where(db.Agent.user_id == str(user_id))
            return stmt

        # --- today -----------------------------------------------------
        today_rows = s.execute(scoped(
            select(db.Call.status, func.count(db.Call.id),
                   func.coalesce(func.sum(db.Call.cost_cents), 0),
                   func.avg(db.Call.sentiment_score))
            .where(db.Call.created_at >= today)
            .group_by(db.Call.status)
        )).all()

        calls_today = sum(r[1] for r in today_rows)
        cost_today = sum(r[2] for r in today_rows)
        resolved_today = sum(r[1] for r in today_rows if r[0] == "resolved")
        sentiments = [r[3] for r in today_rows if r[3] is not None]

        # --- last 7 days, for the delta comparisons --------------------
        week_total, week_resolved, week_cost = s.execute(scoped(
            select(func.count(db.Call.id),
                   func.count(db.Call.id).filter(db.Call.status == "resolved"),
                   func.coalesce(func.sum(db.Call.cost_cents), 0))
            .where(db.Call.created_at >= week_ago)
        )).one()

        # --- hourly volume, today --------------------------------------
        hourly = s.execute(scoped(
            select(func.extract("hour", db.Call.created_at).label("h"),
                   func.count(db.Call.id),
                   func.avg(db.Call.sentiment_score))
            .where(db.Call.created_at >= today)
            .group_by("h").order_by("h")
        )).all()

        # --- cost split by agent ---------------------------------------
        by_agent = s.execute(
            select(db.Agent.persona,
                   func.count(db.Call.id),
                   func.coalesce(func.sum(db.Call.cost_cents), 0),
                   func.avg(db.Call.sentiment_score))
            .join(db.Call, db.Call.agent_id == db.Agent.id)
            .where(db.Call.created_at >= week_ago,
                   *( [db.Agent.user_id == str(user_id)] if user_id else [] ))
            .group_by(db.Agent.persona)
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
                "name": a.persona.title(),
                "role": meta.get("role", "Voice agent"),
                "place": meta.get("place", ""),
                "photo": meta.get("photo", "avatar-kwame"),
                "persona": a.persona,
                "calls_today": n_today,
                "voice": (a.voice_config or {}).get("voice", ""),
            })

    containment = round(resolved_today / calls_today * 100) if calls_today else 0
    week_containment = round(week_resolved / week_total * 100) if week_total else 0
    cost_per_call = (cost_today / calls_today / 100) if calls_today else 0.0
    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else None

    volume = [{"h": f"{int(h):02d}", "v": int(n)} for h, n, _ in hourly]
    sentiment_series = [
        {"h": f"{int(h):02d}", "v": round(float(sc), 3)}
        for h, _, sc in hourly if sc is not None
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
            "avg_sentiment": round(avg_sentiment, 3) if avg_sentiment is not None else None,
            "containment_delta_pts": containment - week_containment,
        },
        "volume": volume,
        "sentiment": sentiment_series,
        "cost_breakdown": [
            {"k": persona.title(), "v": round(cents / 100, 2), "calls": int(n)}
            for persona, n, cents, _ in by_agent
        ],
        "agents": agent_cards,
        "totals": {"week_calls": int(week_total), "week_containment_pct": week_containment},
    }


def get_live_queue(user_id: Optional[str] = None) -> List[dict]:
    """Calls still in progress.

    The engine writes a row on the first turn and only marks it resolved
    at end_call(), so anything still 'abandoned' and recent is a live
    call. There is no telephony layer yet, so in practice this is empty
    until TEL-1 lands — it returns [] rather than inventing waiting
    callers.
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
        waited = int((_now() - call.created_at).total_seconds())
        out.append({
            "id": call.id,
            "num": _mask_number(call.caller_number),
            "cat": (call.transcript or "").splitlines()[0][:40] if call.transcript else "In progress",
            "wait": _mmss(waited),
            "wait_s": waited,
            "agent": agent.persona.title(),
            "hot": waited > 120,
        })
    return out


__all__ = [
    "get_call_history", "get_call_detail", "get_dashboard_stats",
    "get_live_queue", "PERSONA_META",
]
