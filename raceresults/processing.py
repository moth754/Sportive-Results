"""Per-event pipeline: Webscorer data + edits -> result time -> medal -> live feed state -> SMS queue."""

import csv
import io
import json
import threading
import time

from . import db, medals, phone, settings, sms
from .state import state

EDITABLE = ("name", "distance", "category", "gender", "time", "phone")
VIEW_COLUMNS = ("bib", "name", "distance", "category", "gender", "finish_time", "adjusted_time", "result_time",
                "result_secs", "medal", "phone", "purchases", "edited", "fingerprint", "first_seen_at",
                "changed_at", "update_count", "finish_clock")
_lock = threading.Lock()


class EditError(ValueError):
    pass


def auto_result_time(record):
    """The time that counts before any edit: Webscorer's adjusted time when it has one, else the finish time."""
    adjusted = record.get("adjusted", "")
    if adjusted and medals.time_to_seconds(adjusted) is not None:
        return adjusted
    return record.get("time", "")


def original_value(record, field):
    return auto_result_time(record) if field == "time" else record.get(field, "")


def build_view(record, edits, table):
    """Apply a racer's edits and derive result time, medal and finish clock."""
    view = {f: record.get(f, "") for f in ("bib", "name", "distance", "category", "gender", "phone", "purchases")}
    for field in ("name", "distance", "category", "gender", "phone"):
        if field in edits:
            view[field] = edits[field]
    result = edits.get("time", auto_result_time(record))
    seconds = medals.time_to_seconds(result)
    start, finish = medals.time_to_seconds(record.get("start")), medals.time_to_seconds(record.get("time"))
    view.update(
        finish_time=record.get("time", ""),
        adjusted_time=record.get("adjusted", ""),
        result_time=result,
        result_secs=seconds,
        medal=table.medal_for(view["distance"], view["category"], view["gender"], seconds) if table else "",
        edited=json.dumps(sorted(edits)),
        finish_clock=start + finish if start is not None and finish is not None else None,
    )
    return view


def _feed_state(row, view, now):
    """New finishers get first_seen; a changed result counts as an update (the terminal's *** UPDATED ***)."""
    finished = view["result_secs"] is not None
    fingerprint = json.dumps([view["name"], view["distance"], view["category"], view["gender"],
                              view["result_time"], view["medal"]]) if finished else None
    first_seen, changed_at, updates = row["first_seen_at"], row["changed_at"], row["update_count"]
    if fingerprint != row["fingerprint"]:
        if fingerprint is not None and first_seen is None:
            first_seen = now
        elif fingerprint is not None:
            updates += 1
        changed_at = now
    return fingerprint, first_seen, changed_at, updates


def _store_fetch(c, event, fetched):
    racers, meta = fetched
    event_id, changed = event["id"], False
    info = json.dumps(meta["race_info"], sort_keys=True)
    order = json.dumps(meta["group_order"])
    if (info, order, meta["adjusted_field"]) != (event["race_info"], event["group_order"], event["adjusted_field"]):
        c.execute("UPDATE events SET race_info = ?, group_order = ?, adjusted_field = ? WHERE id = ?",
                  (info, order, meta["adjusted_field"], event_id))
        changed = True
    existing = {r["racer_key"]: r for r in
                c.execute("SELECT racer_key, raw, present FROM racers WHERE event_id = ?", (event_id,))}
    seen = set()
    for racer in racers:
        key = racer["key"]
        raw = json.dumps({k: v for k, v in racer.items() if k != "key"}, sort_keys=True)
        seen.add(key)
        old = existing.get(key)
        if old is None:
            c.execute("INSERT INTO racers(event_id, racer_key, raw) VALUES(?, ?, ?)", (event_id, key, raw))
            changed = True
        elif old["raw"] != raw or not old["present"]:
            c.execute("UPDATE racers SET raw = ?, present = 1 WHERE event_id = ? AND racer_key = ?",
                      (raw, event_id, key))
            changed = True
    gone = [(event_id, key) for key, old in existing.items() if key not in seen and old["present"]]
    if gone:
        c.executemany("UPDATE racers SET present = 0 WHERE event_id = ? AND racer_key = ?", gone)
        changed = True
    return changed


def _process(event_id, fetched, s, now):
    with db.transaction() as c:
        event = c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if event is None:
            return False
        changed = _store_fetch(c, event, fetched) if fetched is not None else False
        table = medals.load_table(event["medal_config_id"]) if event["medal_colours"] else None
        edits = {}
        for e in c.execute("SELECT racer_key, field, value FROM edits WHERE event_id = ?", (event_id,)):
            edits.setdefault(e["racer_key"], {})[e["field"]] = e["value"]
        updates = []
        for row in c.execute("SELECT * FROM racers WHERE event_id = ?", (event_id,)).fetchall():
            view = build_view(json.loads(row["raw"]), edits.get(row["racer_key"], {}), table)
            view["fingerprint"], view["first_seen_at"], view["changed_at"], view["update_count"] = \
                _feed_state(row, view, now)
            if any(row[col] != view[col] for col in VIEW_COLUMNS):
                updates.append([view[col] for col in VIEW_COLUMNS] + [event_id, row["racer_key"]])
        if updates:
            c.executemany("UPDATE racers SET " + ", ".join(f"{col} = ?" for col in VIEW_COLUMNS)
                          + " WHERE event_id = ? AND racer_key = ?", updates)
            changed = True
        if changed:
            c.execute("UPDATE events SET data_updated_at = ? WHERE id = ?", (now, event_id))
    return changed


def process_event(event_id, fetched=None):
    """Recompute an event from a new Webscorer fetch (racers, meta) or, with fetched=None, from the cache.

    Called after every fetch and after anything that affects results (edits, medal changes).
    """
    s = settings.get_all()
    with _lock:
        changed = _process(event_id, fetched, s, time.time())
    if changed:
        state.bump(event_id)
    if state.sms_enabled and s["active_event_id"] == event_id:
        sms.enqueue_event(event_id, s)
    return changed


def present_count(event_id):
    return db.query_one("SELECT COUNT(*) AS n FROM racers WHERE event_id = ? AND present = 1", (event_id,))["n"]


# ---------------------------------------------------------------- reading results

def results(event_id):
    """Every entrant for the Results tab. Phone numbers are left out (the edit dialog fetches one when needed)."""
    sms_status = sms.status_by_racer(event_id)
    out = []
    for r in db.query("SELECT * FROM racers WHERE event_id = ? AND present = 1", (event_id,)):
        edited = json.loads(r["edited"])
        item = {
            "key": r["racer_key"], "bib": r["bib"], "name": r["name"], "distance": r["distance"],
            "category": r["category"], "gender": r["gender"], "time": r["result_time"],
            "finish_time": r["finish_time"], "adjusted_time": r["adjusted_time"],
            "finished": r["result_secs"] is not None, "medal": r["medal"], "purchases": r["purchases"],
            "has_phone": bool(r["phone"].strip()), "edited": edited,
            "first_seen": r["first_seen_at"], "changed": r["changed_at"], "updates": r["update_count"],
            "clock": r["finish_clock"], "sms": sms_status.get(r["racer_key"], ""),
        }
        if edited:
            raw = json.loads(r["raw"])
            item["orig"] = {f: original_value(raw, f) for f in edited if f != "phone"}
        out.append(item)
    return out


def edit_options(event_id):
    """Choices for the edit dialog: the event's medal configuration if it has one, otherwise Webscorer's results."""
    event = db.query_one("SELECT medal_config_id FROM events WHERE id = ?", (event_id,))
    if event and event["medal_config_id"]:
        rows = db.query("SELECT distance, category, gender FROM medal_standards WHERE config_id = ? ORDER BY position",
                        (event["medal_config_id"],))
        source = "medals"
    else:
        rows = db.query("SELECT distance, category, gender FROM racers WHERE event_id = ? AND present = 1 "
                        "ORDER BY distance, category, gender", (event_id,))
        source = "results"
    options = {}
    for field in ("distance", "category", "gender"):
        options[field] = list(dict.fromkeys(r[field] for r in rows if r[field]))
    options["source"] = source
    return options


def racer_detail(event_id, key):
    row = db.query_one("SELECT * FROM racers WHERE event_id = ? AND racer_key = ?", (event_id, key))
    if row is None:
        raise EditError("Racer not found")
    raw = json.loads(row["raw"])
    edited = [e["field"] for e in db.query("SELECT field FROM edits WHERE event_id = ? AND racer_key = ?",
                                           (event_id, key))]
    number, problem = phone.normalise(row["phone"], settings.get("phone_region"))
    current = {f: row[f] for f in ("name", "distance", "category", "gender", "phone")}
    current["time"] = row["result_time"]
    return {
        "key": key, "bib": row["bib"], "current": current,
        "original": {f: original_value(raw, f) for f in EDITABLE},
        "finish_time": raw.get("time", ""), "adjusted_time": raw.get("adjusted", ""),
        "edited": sorted(edited), "medal": row["medal"],
        "phone_check": {"ok": bool(number), "number": number or "", "error": problem or ""},
        "options": edit_options(event_id),
    }


# ---------------------------------------------------------------- edits

def save_edits(event_id, key, fields):
    """Store overrides for a racer. A value equal to Webscorer's removes that override."""
    row = db.query_one("SELECT raw FROM racers WHERE event_id = ? AND racer_key = ?", (event_id, key))
    if row is None:
        raise EditError("Racer not found")
    raw = json.loads(row["raw"])
    now = time.time()
    with db.transaction() as c:
        for field in EDITABLE:
            if field not in fields:
                continue
            value = str(fields[field] or "")
            value = value.strip() if field == "phone" else " ".join(value.split())
            original = original_value(raw, field)
            if field == "time" and value and not medals.valid_result_time(value):
                raise EditError(f"'{value}' isn't a time: use H:MM:SS (e.g. 4:26:32.8) or MM:SS")
            if field in ("name", "distance", "gender") and not value and original:
                raise EditError(f"{field.title()} can't be blank")
            if value == original or (field == "time" and not value):
                c.execute("DELETE FROM edits WHERE event_id = ? AND racer_key = ? AND field = ?",
                          (event_id, key, field))
            else:
                c.execute("INSERT INTO edits(event_id, racer_key, field, value, edited_at) VALUES(?, ?, ?, ?, ?) "
                          "ON CONFLICT(event_id, racer_key, field) DO UPDATE SET value = excluded.value, "
                          "edited_at = excluded.edited_at", (event_id, key, field, value, now))
    process_event(event_id)
    state.activity(f"Result edited for bib {raw.get('bib') or key}")
    return racer_detail(event_id, key)


def revert_edits(event_id, key):
    db.execute("DELETE FROM edits WHERE event_id = ? AND racer_key = ?", (event_id, key))
    process_event(event_id)
    state.activity(f"Edits reverted for {key}")
    return racer_detail(event_id, key)


def preview_medal(event_id, distance, category, gender, time_text):
    """Medal for proposed values in the edit dialog (not saved)."""
    event = db.query_one("SELECT medal_config_id, medal_colours FROM events WHERE id = ?", (event_id,))
    if event is None or not event["medal_colours"]:
        return {"medal": "", "note": "Medal colours are off for this event"}
    if not event["medal_config_id"]:
        return {"medal": "", "note": "This event has no medal configuration"}
    table = medals.load_table(event["medal_config_id"])
    if not table.has(distance, category, gender):
        return {"medal": "", "note": "No medal standard for this distance / category / gender"}
    seconds = medals.time_to_seconds(time_text)
    if seconds is None:
        return {"medal": "", "note": "Not a finish time"}
    medal = table.medal_for(distance, category, gender, seconds)
    return {"medal": medal, "note": "" if medal else "Outside every medal time"}


# ---------------------------------------------------------------- export

def export_csv(event_id):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Bib", "Name", "Distance", "Category", "Gender", "Time", "Finish time", "Adjusted time",
                     "Medal", "Phone", "Purchases", "Edited", "First seen"])
    rows = db.query("SELECT * FROM racers WHERE event_id = ? AND present = 1 "
                    "ORDER BY distance, result_secs IS NULL, result_secs, name", (event_id,))
    for r in rows:
        seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["first_seen_at"])) if r["first_seen_at"] else ""
        writer.writerow([r["bib"], r["name"], r["distance"], r["category"], r["gender"], r["result_time"],
                         r["finish_time"], r["adjusted_time"], r["medal"], r["phone"], r["purchases"],
                         ", ".join(json.loads(r["edited"])), seen])
    return out.getvalue()
