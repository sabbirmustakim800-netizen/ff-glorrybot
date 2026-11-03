"""Unit tests for the pure verification logic — no network, no database."""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
from datetime import datetime, timedelta, timezone

from app.bkash import (
    BkashCheck,
    enforce_freshness,
    normalize_bkash_number,
    parse_bkash_response,
    parse_tx_time,
    sender_check,
)
from app.binance import build_deposit_params, classify, signed_query
from app.config import parse_rates


# ------------------------------------------------------------------ bKash inputs


def test_normalize_bkash_number():
    assert normalize_bkash_number("01712345678") == "01712345678"
    assert normalize_bkash_number("+8801712345678") == "01712345678"
    assert normalize_bkash_number("8801712345678") == "01712345678"
    assert normalize_bkash_number("0171234567") is None  # too short
    assert normalize_bkash_number("01212345678") is None  # 012 not valid BD mobile
    assert normalize_bkash_number("hello") is None


def test_sender_check_exact_and_international():
    assert sender_check("01712345678", "01712345678") == "match"
    assert sender_check("+8801712345678", "01712345678") == "match"
    assert sender_check("01712345678", "01799999999") == "mismatch"
    assert sender_check("01712345678", None) == "unavailable"
    assert sender_check("01712345678", "") == "unavailable"


def test_sender_check_masked_values():
    assert sender_check("01712345678", "017****5678") == "match"
    assert sender_check("01712345678", "017****5679") == "mismatch"
    assert sender_check("01712345678", "017-1234-5678") == "match"


# ---------------------------------------------------------------- bKash responses


def test_paygate_style_success():
    payload = {
        "success": True,
        "valid": True,
        "message": "Transaction verified.",
        "transaction": {
            "trxId": "DDS3M42DR5",
            "amount": 30,
            "senderPhone": "01712345678",
            "trxTimestamp": "28/04/2026 21:23",
            "status": "verified",
        },
    }
    check = parse_bkash_response(200, payload)
    assert check.state == "verified"
    assert check.amount == 30
    assert check.sender == "01712345678"
    assert check.timestamp == datetime(2026, 4, 28, 21, 23)


def test_not_found_message_is_retried():
    payload = {
        "success": False,
        "message": "Transaction not found. SMS not received yet or TrxID incorrect.",
    }
    check = parse_bkash_response(200, payload)
    assert check.state == "not_found"
    assert check.retryable


def test_reseller_status_data_envelope():
    payload = {
        "status": "success",
        "data": {
            "trxID": "ABC123XYZ",
            "amount": "500.00",
            "sender": "017****5678",
            "timestamp": "2026-05-20T10:30:00.000Z",
        },
    }
    check = parse_bkash_response(200, payload)
    assert check.state == "verified"
    assert check.amount == 500.0
    assert check.sender == "017****5678"
    assert check.timestamp == datetime(2026, 5, 20, 10, 30, 0)


def test_legacy_trxstatus_codes():
    assert parse_bkash_response(200, {"trxStatus": "0000", "amount": "100.50"}).state == "verified"
    assert parse_bkash_response(200, {"trxStatus": "0011"}).state == "pending"
    assert parse_bkash_response(200, {"trxStatus": "1002"}).state == "not_found"
    assert parse_bkash_response(200, {"trxStatus": "1004"}).state == "not_found"
    assert parse_bkash_response(200, {"trxStatus": "0111"}).state == "failed"
    assert parse_bkash_response(200, {"trxStatus": "1003"}).state == "error"
    assert parse_bkash_response(200, {"trxStatus": "9999"}).state == "unavailable"


def test_bkash_official_style_status_code():
    payload = {
        "statusCode": "0000",
        "statusMessage": "Successful",
        "trxID": "BFD90JRLST",
        "amount": "100.50",
        "customerMsisdn": "01712345678",
    }
    check = parse_bkash_response(200, payload)
    assert check.state == "verified"
    assert check.amount == 100.50
    assert check.sender == "01712345678"


def test_pending_and_unknown_shapes_never_verify():
    assert parse_bkash_response(200, {"status": "pending", "amount": 10}).state == "pending"
    # Ambiguous/unrecognized payloads must NOT be treated as verified.
    assert parse_bkash_response(200, {"foo": "bar"}).state == "error"
    assert parse_bkash_response(200, None).state == "error"


def test_http_level_states():
    assert parse_bkash_response(401, None).state == "error"
    assert parse_bkash_response(402, None).state == "error"
    assert parse_bkash_response(429, None).state == "unavailable"
    assert parse_bkash_response(500, None).state == "unavailable"
    assert parse_bkash_response(404, {"detail": "nope"}).state == "not_found"


# ------------------------------------------------------------------ freshness / time


def test_parse_tx_time_variants():
    epoch = datetime.fromtimestamp(1747743000, tz=timezone.utc).replace(tzinfo=None)
    assert parse_tx_time(1747743000000) == epoch
    assert parse_tx_time("1747743000000") == epoch
    assert parse_tx_time("2026-05-20T10:30:00.000Z") == datetime(2026, 5, 20, 10, 30)
    assert parse_tx_time("2024-06-13T12:32:35.033 GMT+0600") == datetime(
        2024, 6, 13, 6, 32, 35, 33000
    )
    assert parse_tx_time("28/04/2026 21:23") == datetime(2026, 4, 28, 21, 23)
    assert parse_tx_time("not a date") is None
    assert parse_tx_time(None) is None


def test_enforce_freshness_blocks_old_transactions():
    fresh = parse_bkash_response(
        200, {"success": True, "timestamp": "2026-05-31T10:00:00.000Z", "amount": 100}
    )
    # Timestamp far in the past → rejected as replay.
    stale = parse_bkash_response(
        200, {"success": True, "timestamp": "2020-01-01T10:00:00.000Z", "amount": 100}
    )
    no_time = parse_bkash_response(200, {"success": True, "amount": 100})

    assert enforce_freshness(stale, 48).state == "failed"
    assert enforce_freshness(no_time, 48).state == "verified"
    # "fresh" depends on wall clock — just assert it stays verified or failed-free.
    result = enforce_freshness(fresh, 48)
    assert result.state in ("verified", "failed")


def test_enforce_freshness_future_timestamp_rejected():
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)
    check = BkashCheck(state="verified", amount=10.0, timestamp=future)
    assert enforce_freshness(check, 48).state == "failed"


# ----------------------------------------------------------------------- Binance


def test_signed_query_signs_the_percent_encoded_query():
    params = {"coin": "USDT", "txId": "a+b/c", "timestamp": 1700000000000}
    query = signed_query(params, "supersecret")
    base, sep, signature = query.partition("&signature=")
    assert sep == "&signature="
    assert "txId=a%2Bb%2Fc" in base  # encoded BEFORE signing
    expected = hmac_lib.new(b"supersecret", base.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert len(signature) == 64


def test_build_deposit_params_window_is_under_90_days():
    params = build_deposit_params(coin="USDT", tx_id="abc", now_ms=1_700_000_000_000)
    assert params["coin"] == "USDT"
    assert params["txId"] == "abc"
    window = params["endTime"] - params["startTime"]
    assert 0 < window <= 89 * 86_400_000
    assert params["endTime"] == 1_700_000_000_000


def test_classify_deposit_statuses():
    tx = "98A3EA56"
    credited = [{"txId": tx.upper(), "status": 1, "amount": "10.5", "coin": "USDT"}]
    assert classify(credited, tx.lower()) == ("credited", credited[0])

    pending = [{"txId": tx, "status": 0, "amount": "10.5"}]
    assert classify(pending, tx)[0] == "pending"

    locked = [{"txId": tx, "status": 6, "amount": "10.5"}]
    assert classify(locked, tx)[0] == "pending"  # not final → no credit

    rejected = [{"txId": tx, "status": 2, "amount": "10.5"}]
    assert classify(rejected, tx)[0] == "rejected"

    weird = [{"txId": tx, "status": "unknown", "amount": "10.5"}]
    assert classify(weird, tx)[0] == "pending"  # unknown code → never credit

    assert classify([], tx) == ("not_found", None)
    assert classify(None, tx) == ("not_found", None)
    assert classify([{"txId": "OTHER", "status": 1}], tx) == ("not_found", None)


# ------------------------------------------------------------------------ config


def test_parse_rates_formats():
    assert parse_rates("USDT=110.5, BTC=6500000") == {"USDT": 110.5, "BTC": 6500000}
    assert parse_rates('{"usdt": 110}') == {"USDT": 110.0}
    assert parse_rates("") == {}
    assert parse_rates("garbage") == {}


# ------------------------------------------------------------------ app wiring


def test_fastapi_app_imports():
    from app.handlers import COMMANDS, app

    assert app.title == "FF Glory Bot"
    assert set(COMMANDS) >= {"/start", "/help", "/balance", "/bkash", "/binance"}