"""Result texts: who to text, what to say, a queue that never double-sends, and Twilio's REST API."""

import os
import re
import socket
import threading
import time

import phonenumbers
import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from . import db, phone, settings
from .state import state

TWILIO_URL = os.environ.get("RACERESULTS_TWILIO_URL", "https://api.twilio.com/2010-04-01")
MAX_ATTEMPTS = 5
RETRY_DELAYS = (15, 30, 60, 120, 240)
# Serialises queueing and claiming so the web app and the sender never race each other.
_lock = threading.Lock()


class SmsError(ValueError):
    pass


def build_message(bib, distance, category, gender, result_time, medal):
    """Same wording as terminal_results.build_sms_message."""
    message = f"{bib} - {distance} - {category} - {gender} - {result_time}"
    if medal:
        message += f" - {medal}"
    return message


def eligible(racer, medals_on):
    """As terminal_results.should_send_sms: finished, has a phone, and a medal when medal colours are on."""
    if racer["result_secs"] is None or not (racer["phone"] or "").strip():
        return False
    return bool(racer["medal"]) or not medals_on


def _blocked(entries, message, raw_number, e164):
    """Has this text already been dealt with?

    Queued, sending or sent: yes. Failed to the same number: yes, so a bad number is
    only retried once it has been corrected. Cancelled or superseded: no.
    `message=None` matches any wording (used for the once-per-racer info text).
    """
    for entry in entries:
        if message is not None and entry["message"] != message:
            continue
        if entry["status"] in ("pending", "sending", "sent"):
            return True
        if entry["status"] == "failed" and (
            entry["raw_number"] == raw_number or (e164 and entry["to_number"] == e164)
        ):
            return True
    return False


def _plan(c, event_id, s):
    """The texts that should be queued for an event right now."""
    event = c.execute("SELECT medal_colours, info_on, info_message FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        return []
    medals_on = bool(event["medal_colours"])
    info_text = event["info_message"].strip() if event["info_on"] else ""
    log = {}
    for entry in c.execute("SELECT * FROM sms_log WHERE event_id = ?", (event_id,)):
        log.setdefault((entry["racer_key"], entry["kind"]), []).append(entry)
    actions = []
    racers = c.execute(
        "SELECT * FROM racers WHERE event_id = ? AND present = 1 AND result_secs IS NOT NULL "
        "ORDER BY first_seen_at, finish_clock",
        (event_id,),
    ).fetchall()
    for racer in racers:
        if not eligible(racer, medals_on):
            continue
        message = build_message(racer["bib"], racer["distance"], racer["category"], racer["gender"],
                                racer["result_time"], racer["medal"])
        raw = racer["phone"].strip()
        e164, error = phone.normalise(raw, s["phone_region"])
        entries = log.get((racer["racer_key"], "result"), [])
        if _blocked(entries, message, raw, e164):
            continue
        send_info = bool(info_text) and not error and not _blocked(
            log.get((racer["racer_key"], "info"), []), None, raw, e164)
        actions.append({
            "racer": racer, "message": message, "raw": raw, "e164": e164, "error": error,
            "supersede": [e["id"] for e in entries if e["status"] == "pending"],
            "info": info_text if send_info else "",
        })
    return actions


def twilio_ready(s):
    return bool(s["twilio_account_sid"] and s["twilio_auth_token"]
                and (s["twilio_messaging_service_sid"] or s["twilio_from"]))


def preview(event_id):
    """What switching SMS on would do right now (for the confirmation pop-up)."""
    s = settings.get_all()
    actions = _plan(db.conn(), event_id, s) if event_id else []
    return {
        "texts": sum(1 for a in actions if not a["error"]),
        "invalid": sum(1 for a in actions if a["error"]),
        "info": sum(1 for a in actions if a["info"]),
        "twilio_ready": twilio_ready(s),
        "medal_colours": bool(db.query_one("SELECT medal_colours FROM events WHERE id = ?", (event_id,))["medal_colours"])
        if event_id else False,
    }


def _insert(c, event_id, racer, kind, message, raw, e164, status, error, now):
    c.execute(
        "INSERT INTO sms_log(event_id, racer_key, bib, kind, message, raw_number, to_number, status, error, "
        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, racer["racer_key"], racer["bib"], kind, message, raw, e164 or "", status, error, now, now),
    )


def enqueue_event(event_id, s=None):
    """Queue every text that's due for this event. Safe to call as often as you like."""
    if not state.sms_enabled:
        return 0
    s = s or settings.get_all()
    now = time.time()
    queued = 0
    with _lock, db.transaction() as c:
        for action in _plan(c, event_id, s):
            racer = action["racer"]
            if action["supersede"]:
                c.executemany("UPDATE sms_log SET status = 'superseded', updated_at = ? WHERE id = ?",
                              [(now, i) for i in action["supersede"]])
            if action["error"]:
                # Invalid number: logged once and never sent (until the number is corrected)
                _insert(c, event_id, racer, "result", action["message"], action["raw"], "", "failed",
                        action["error"], now)
                state.activity(f"No text for bib {racer['bib']}: {action['error']}", "warn")
                continue
            _insert(c, event_id, racer, "result", action["message"], action["raw"], action["e164"], "pending", "", now)
            queued += 1
            if action["info"]:
                _insert(c, event_id, racer, "info", action["info"], action["raw"], action["e164"], "pending", "", now)
    if queued:
        state.bump(event_id)
        state.wake_sms.set()
    return queued


def set_enabled(on, event_id=None):
    """The header switch. Turning it off cancels anything still queued."""
    cancelled = 0
    with _lock:
        state.sms_enabled = bool(on)
        if not on:
            cancelled = db.execute("UPDATE sms_log SET status = 'cancelled', updated_at = ? WHERE status = 'pending'",
                                   (time.time(),)).rowcount
    if on:
        state.activity("Text messages switched ON")
        return enqueue_event(event_id) if event_id else 0
    state.activity("Text messages switched OFF" + (f" ({cancelled} queued texts cancelled)" if cancelled else ""))
    if event_id and cancelled:
        state.bump(event_id)
    return 0


# ---------------------------------------------------------------- sending

class Outcome:
    def __init__(self, ok, error="", retry=False, sid=""):
        self.ok, self.error, self.retry, self.sid = ok, error, retry, sid


def _never_reached_twilio(exc):
    """True only when the request certainly wasn't delivered, so retrying can't double-send."""
    if isinstance(exc, (requests.ConnectTimeout, requests.exceptions.SSLError)):
        return True
    if isinstance(exc, requests.ConnectionError):
        reason = exc.args[0] if exc.args else None
        reason = getattr(reason, "reason", reason)  # urllib3 MaxRetryError -> underlying cause
        return isinstance(reason, (NewConnectionError, ConnectTimeoutError))
    return False


def _twilio_error(response):
    try:
        body = response.json()
    except ValueError:
        body = {}
    code, message = body.get("code"), body.get("message") or response.reason
    if response.status_code >= 500:
        return f"Twilio server error (HTTP {response.status_code}): check the Twilio console before resending"
    return f"Twilio error {code}: {message}" if code else f"Twilio HTTP {response.status_code}: {message}"


def _sender_id(s):
    """The From number in E.164 (the old Config.csv stored it without the +), or an alphanumeric sender ID as-is."""
    sender = s["twilio_from"].strip()
    if not re.fullmatch(r"\+?[\d\s()\-]+", sender):
        return sender
    try:
        number = phonenumbers.parse(re.sub(r"[^\d+]", "", sender), s["phone_region"])
    except phonenumbers.NumberParseException:
        return sender
    if not phonenumbers.is_valid_number(number):
        return sender
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def twilio_send(s, to, body, timeout=(6, 20)):
    if not twilio_ready(s):
        return Outcome(False, "Twilio isn't fully set up (Configuration tab)")
    data = {"To": to, "Body": body}
    if s["twilio_messaging_service_sid"]:
        data["MessagingServiceSid"] = s["twilio_messaging_service_sid"]
    else:
        data["From"] = _sender_id(s)
    url = f"{TWILIO_URL}/Accounts/{s['twilio_account_sid']}/Messages.json"
    try:
        response = requests.post(url, data=data, auth=(s["twilio_account_sid"], s["twilio_auth_token"]),
                                 timeout=timeout)
    except requests.RequestException as exc:
        if _never_reached_twilio(exc):
            return Outcome(False, "Couldn't connect to Twilio (will retry)", retry=True)
        if isinstance(exc, requests.Timeout):
            return Outcome(False, "Twilio didn't answer in time: the text may have been sent. "
                                  "Check the Twilio console before resending")
        return Outcome(False, f"Connection to Twilio dropped ({type(exc).__name__}): "
                              "check the Twilio console before resending")
    if response.status_code in (200, 201):
        try:
            sid = response.json().get("sid", "")
        except ValueError:
            sid = ""
        return Outcome(True, sid=sid)
    if response.status_code == 429:
        return Outcome(False, "Twilio rate limit (will retry)", retry=True)
    return Outcome(False, _twilio_error(response))


def send_next():
    """Send the oldest due text. Returns True if one was processed."""
    if not state.sms_enabled:
        return False
    s = settings.get_all()
    now = time.time()
    with _lock, db.transaction() as c:
        row = c.execute("SELECT * FROM sms_log WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT 1",
                        (now,)).fetchone()
        if row is None:
            return False
        # Marked 'sending' before Twilio is called: after a crash it is never sent a second time.
        c.execute("UPDATE sms_log SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                  (now, row["id"]))

    outcome = twilio_send(s, row["to_number"], row["message"])
    attempts = row["attempts"] + 1
    now = time.time()
    if outcome.ok:
        status, error, next_at = "sent", "", 0
    elif outcome.retry and attempts < MAX_ATTEMPTS:
        status, error, next_at = "pending", outcome.error, now + RETRY_DELAYS[attempts - 1]
    else:
        status, error, next_at = "failed", outcome.error, 0
    with _lock:
        if status == "pending" and not state.sms_enabled:
            status = "cancelled"
        db.execute("UPDATE sms_log SET status = ?, error = ?, twilio_sid = ?, next_attempt_at = ?, updated_at = ? "
                   "WHERE id = ?", (status, error, outcome.sid, next_at, now, row["id"]))
    what = "info text" if row["kind"] == "info" else "result text"
    if status == "sent":
        state.activity(f"Sent {what} to bib {row['bib']}")
    elif status == "failed":
        state.activity(f"Failed to send {what} to bib {row['bib']}: {error}", "error")
    else:
        state.activity(f"Couldn't send {what} to bib {row['bib']}: {error}", "warn")
    state.bump(row["event_id"])
    return True


def resend(sms_id):
    """Queue a fresh copy of a logged text, to the racer's current number."""
    if not state.sms_enabled:
        raise SmsError("Switch text messages on first")
    s = settings.get_all()
    now = time.time()
    with _lock, db.transaction() as c:
        entry = c.execute("SELECT * FROM sms_log WHERE id = ?", (sms_id,)).fetchone()
        if entry is None:
            raise SmsError("Message not found")
        if entry["status"] == "pending":
            raise SmsError("That text is already queued")
        if entry["status"] == "sending" and now - entry["updated_at"] < 120:
            raise SmsError("That text is being sent right now")
        racer = c.execute("SELECT racer_key, bib, phone FROM racers WHERE event_id = ? AND racer_key = ?",
                          (entry["event_id"], entry["racer_key"])).fetchone()
        raw = racer["phone"].strip() if racer else entry["raw_number"]
        e164, error = phone.normalise(raw, s["phone_region"])
        if error:
            raise SmsError(error)
        target = racer or {"racer_key": entry["racer_key"], "bib": entry["bib"]}
        _insert(c, entry["event_id"], target, entry["kind"], entry["message"], raw, e164, "pending", "", now)
    state.activity(f"Resending text to bib {entry['bib']}")
    state.bump(entry["event_id"])
    state.wake_sms.set()


def send_test(to):
    s = settings.get_all()
    e164, error = phone.normalise(to, s["phone_region"])
    if error:
        raise SmsError(error)
    sender = s["timing_company_name"] or "Race Results"
    outcome = twilio_send(s, e164, f"Test message from {sender} ({socket.gethostname()}). Texts are working.")
    if not outcome.ok:
        raise SmsError(outcome.error)
    state.activity(f"Test text sent to {e164}")
    return e164


# ---------------------------------------------------------------- reporting

def counts(event_id):
    rows = db.query("SELECT status, COUNT(*) AS n FROM sms_log WHERE event_id = ? GROUP BY status", (event_id,))
    return {r["status"]: r["n"] for r in rows}


def status_by_racer(event_id):
    """Most relevant result-text status per racer, for the icon in the results table."""
    out = {}
    for row in db.query("SELECT racer_key, status FROM sms_log WHERE event_id = ? AND kind = 'result' ORDER BY id",
                        (event_id,)):
        if row["status"] in ("cancelled", "superseded") and row["racer_key"] in out:
            continue
        out[row["racer_key"]] = row["status"]
    return out


def log_entries(event_id, limit=500):
    rows = db.query(
        "SELECT id, bib, kind, message, to_number, raw_number, status, error, attempts, created_at, updated_at "
        "FROM sms_log WHERE event_id = ? ORDER BY id DESC LIMIT ?", (event_id, limit))
    return [dict(r) for r in rows]
