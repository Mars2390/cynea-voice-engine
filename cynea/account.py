"""What a customer's account holds: take it away, or take it with you."""

from __future__ import annotations

import logging

from cynea import apikeys, dashboard_data, db, recordings, webhooks

log = logging.getLogger("cynea.account")


def _iso(dt):
    return dashboard_data._iso(dt)


def export(user_id: str) -> dict:
    """Everything stored about this account, as JSON.

    Secrets are left out: no password hash, no key hashes, no webhook signing
    secrets. Full caller numbers and transcripts are in, since they are the
    customer's own call records. Audio is listed rather than inlined; each
    recording can be fetched from /calls/{id}/recording.
    """
    user = db.get_user_by_id(user_id)
    agents = []
    for agent in db.get_agents_by_user(user_id):
        calls = []
        for call in db.get_calls_by_agent(agent.id, limit=100_000):
            calls.append({
                "id": call.id, "caller_number": call.caller_number,
                "duration_s": call.duration_s, "transcript": call.transcript,
                "sentiment_score": call.sentiment_score, "cost_cents": call.cost_cents,
                "status": call.status, "created_at": _iso(call.created_at),
                "has_recording": bool(call.audio_url),
            })
        agents.append({
            "id": agent.id, "name": agent.name, "persona": agent.persona,
            "voice_config": agent.voice_config or {}, "created_at": _iso(agent.created_at),
            "prompt_versions": [
                {"version": v.version, "content": v.content, "created_at": _iso(v.created_at)}
                for v in db.get_prompt_versions(agent.id, limit=100_000)],
            "calls": calls,
        })
    return {
        "exported_at": _iso(db._now()),
        "account": {"id": user.id, "email": user.email, "name": user.full_name,
                    "company": user.company, "created_at": _iso(user.created_at),
                    "email_verified_at": _iso(user.email_verified_at)},
        "agents": agents,
        "api_keys": [{"id": k.id, "name": k.name, "prefix": k.prefix,
                      "created_at": _iso(k.created_at), "last_used_at": _iso(k.last_used_at)}
                     for k in (apikeys.list_for(user_id) if db.table_ready("api_keys") else [])],
        "webhooks": [{"id": h.id, "url": h.url, "created_at": _iso(h.created_at)}
                     for h in webhooks.list_for(user_id)],
    }


def delete_account(user_id: str) -> int:
    """Delete the account and its stored audio. Returns how many recordings went.

    The audio goes first. Once the rows are gone nothing points at those files,
    so a failure the other way round would leave a deleted customer's calls in
    storage for good. If storage refuses, nothing is deleted and the customer
    can try again: recordings.StorageError reaches the caller.
    """
    audio = []
    for agent in db.get_agents_by_user(user_id):
        audio += [c.audio_url for c in db.get_calls_by_agent(agent.id, limit=100_000)
                  if c.audio_url]
    for url in audio:
        recordings.delete(url)
    db.delete_user(user_id)
    log.info("account deleted, %d recording(s) removed", len(audio))
    return len(audio)
