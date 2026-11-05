"""The Twilio voice webhook, reached the way Vercel reaches it.

Requests are signed with Twilio's own RequestValidator, so a pass here means the
real algorithm accepted them. Every request goes through api/index.py, because the
/api mount is part of what has to work. No network and no database.
"""

from __future__ import annotations

import importlib.util
import pathlib
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATH = "/api/tel/twilio/voice"
PUBLIC_URL = "https://cynea-voice-engine.vercel.app" + PATH
TOKEN = "test-auth-token"
SID = "ACtest"
CALL = {"CallSid": "CA123", "From": "+15550100", "To": "+17372508034", "AccountSid": SID}


def sign(params, url=PUBLIC_URL, token=TOKEN):
    return RequestValidator(token).compute_signature(url, params)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SID)
    monkeypatch.delenv("TWILIO_VOICE_URL", raising=False)
    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / "api" / "index.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return TestClient(entry.app)


def post(client, params=CALL, signature=None, **kwargs):
    headers = {} if signature == "" else {"X-Twilio-Signature": signature or sign(params)}
    headers.update(kwargs.pop("headers", {}))
    return client.post(PATH, data=params, headers=headers, **kwargs)


def test_a_signed_request_gets_valid_twiml(client):
    r = post(client)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/xml"
    root = ET.fromstring(r.text)
    assert root.tag == "Response"
    assert [el.tag for el in root] == ["Say", "Pause", "Say"]
    assert all(el.text.strip() for el in root if el.tag == "Say")
    assert 0 < int(root[1].attrib["length"]) <= 2


def test_it_never_asks_for_a_stream_and_says_it_is_an_ai(client):
    text = post(client).text
    assert "<Stream" not in text and "<Connect" not in text
    assert "AI" in text


def test_a_field_this_route_does_not_read_is_still_covered(client):
    params = {**CALL, "CallerCity": "Austin", "Direction": "inbound"}
    assert post(client, params).status_code == 200


@pytest.mark.parametrize("signature", ["", "bm90LWEtc2lnbmF0dXJl", "ünïcode"])
def test_missing_or_wrong_signature_is_403(client, signature):
    headers = {"X-Twilio-Signature": signature.encode("latin-1")} if signature else {}
    r = client.post(PATH, data=CALL, headers=headers)
    assert r.status_code == 403
    assert "<Response" not in r.text


def test_a_changed_field_breaks_the_signature(client):
    good = sign(CALL)
    assert post(client, {**CALL, "From": "+15559999"}, signature=good).status_code == 403


def test_the_wrong_token_is_403(client):
    assert post(client, signature=sign(CALL, token="someone-elses")).status_code == 403


def test_a_signature_for_another_url_is_403(client):
    evil = sign(CALL, url="https://evil.example" + PATH)
    assert post(client, signature=evil).status_code == 403


def test_a_forged_host_cannot_make_a_forged_url_pass(client):
    """The check uses the configured URL, so what the sender claims is ignored."""
    evil = sign(CALL, url="https://evil.example" + PATH)
    assert post(client, signature=evil, headers={"Host": "evil.example"}).status_code == 403
    assert post(client, headers={"Host": "evil.example"}).status_code == 200


def test_the_url_comes_from_configuration_when_it_is_overridden(client, monkeypatch):
    moved = "https://cynea.example/api/tel/twilio/voice"
    monkeypatch.setenv("TWILIO_VOICE_URL", moved)
    assert post(client, signature=sign(CALL, url=moved)).status_code == 200
    assert post(client).status_code == 403


def test_without_a_token_every_call_is_refused(client, monkeypatch):
    """An empty key is a key anyone can sign with."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    assert post(client, signature=sign(CALL, token="")).status_code == 503


def test_a_call_on_another_account_is_403(client):
    params = {**CALL, "AccountSid": "ACother"}
    assert post(client, params).status_code == 403


def test_only_post_is_allowed(client):
    assert client.get(PATH).status_code == 405


def test_a_body_in_another_format_is_refused(client):
    r = client.post(PATH, json=CALL, headers={"X-Twilio-Signature": sign(CALL)})
    assert r.status_code == 403


def test_the_call_is_logged_only_once_it_is_verified(client, capsys):
    post(client, signature="bm90LWEtc2lnbmF0dXJl")
    assert "CA123" not in capsys.readouterr().out

    post(client)
    out = capsys.readouterr().out
    assert "CallSid=CA123" in out and "From=+15550100" in out and "To=+17372508034" in out


def test_a_forged_request_cannot_write_lines_into_the_log(client, capsys):
    params = {**CALL, "From": "+1\n[twilio] fake line"}
    post(client, params)
    assert "\n[twilio] fake line" not in capsys.readouterr().out


def test_it_answers_without_the_database(client, monkeypatch):
    """Neon's cold start alone is seconds, and Twilio is waiting on the line."""
    from cynea import db

    def down(*a, **k):
        raise AssertionError("the webhook touched the database")
    monkeypatch.setattr(db, "session_scope", down)
    monkeypatch.setattr(db, "get_engine", down)
    assert post(client).status_code == 200
