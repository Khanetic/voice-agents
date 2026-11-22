import hashlib
import hmac
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core import security
from app.core.config import Settings, get_settings
from app.main import app

URL = "http://supabase.test"
KEY = ec.generate_private_key(ec.SECP256R1())  # plays Supabase's signing key
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())  # an attacker's key

client = TestClient(app)


def fake_settings() -> Settings:
    return Settings(_env_file=None, supabase_url=URL, tool_webhook_secret="s3cret")


@pytest.fixture(autouse=True)
def fake_supabase(monkeypatch):
    app.dependency_overrides[get_settings] = fake_settings
    monkeypatch.setattr(security, "_signing_key", lambda token, s: KEY.public_key())
    yield
    app.dependency_overrides.clear()


def make_token(key=KEY, algorithm="ES256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "user-1",
        "email": "owner@example.test",
        "aud": "authenticated",
        "iss": f"{URL}/auth/v1",
        "iat": now,
        "exp": now + 3600,
    } | overrides
    return jwt.encode(claims, key, algorithm=algorithm)


def get_me(token: str):
    return client.get("/me", headers={"Authorization": f"Bearer {token}"})


# --- owner JWT --------------------------------------------------------------
def test_valid_token_returns_user():
    r = get_me(make_token())
    assert r.status_code == 200
    assert r.json() == {"id": "user-1", "email": "owner@example.test"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}, {"Authorization": "x"}],
)
def test_missing_or_malformed_header(headers):
    r = client.get("/me", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "token",
    [
        make_token(exp=int(time.time()) - 10),  # expired
        make_token(aud="anon"),  # wrong audience
        make_token(iss="https://evil.example/auth/v1"),  # wrong issuer
        make_token(key=OTHER_KEY),  # signed by someone else
        make_token(key="x" * 32, algorithm="HS256"),  # algorithm downgrade
        "not.a.jwt",
    ],
    ids=["expired", "audience", "issuer", "other_key", "hs256", "garbage"],
)
def test_bad_tokens_rejected(token):
    r = get_me(token)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid token"}  # no internal details leaked


def test_missing_sub_rejected():
    claims = {"aud": "authenticated", "iss": f"{URL}/auth/v1", "iat": 1, "exp": 2**31}
    assert get_me(jwt.encode(claims, KEY, algorithm="ES256")).status_code == 401


# --- tool secret ------------------------------------------------------------
def test_tool_secret_ok():
    security.verify_tool_secret(fake_settings(), "s3cret")  # no exception


@pytest.mark.parametrize("value", [None, "", "wrong", "s3cret ", "s3crét"])
def test_tool_secret_bad(value):
    with pytest.raises(HTTPException) as e:
        security.verify_tool_secret(fake_settings(), value)
    assert e.value.status_code == 401


def test_tool_secret_unconfigured_fails_closed():
    s = Settings(_env_file=None, tool_webhook_secret="")
    with pytest.raises(HTTPException) as e:
        security.verify_tool_secret(s, "")
    assert e.value.status_code == 503


# --- webhook signature ------------------------------------------------------
SECRET = "whsec"
BODY = b'{"type":"post_call_transcription"}'


def sign(body: bytes, ts: int, secret: str = SECRET) -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={mac}"


def test_signature_valid():
    now = 1_800_000_000
    assert security.verify_signature(BODY, sign(BODY, now), SECRET, now=now)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        sign(BODY, 1_800_000_000, secret="wrong"),  # wrong secret
        sign(b"{}", 1_800_000_000),  # body changed after signing
        sign(BODY, 1_800_000_000 - 301),  # replay: older than 5 minutes
        "t=abc,v0=00",
    ],
    ids=["none", "empty", "garbage", "secret", "tampered", "replay", "bad_ts"],
)
def test_signature_invalid(header):
    assert not security.verify_signature(BODY, header, SECRET, now=1_800_000_000)
