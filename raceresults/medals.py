"""Medal standards: time parsing, medal matching, CSV import/export and saved configurations."""

import csv
import io
import re
import time

from . import db

LEVELS = ("gold", "silver", "bronze", "finisher")
KEY_FIELDS = ("distance", "category", "gender")
CSV_HEADER = ["Distance", "Category", "Gender", "Gold", "Silver", "Bronze", "Finisher"]

# Webscorer times: "4:26:32.8", "26:45.0", "08:35:00"
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")
# Strict forms for things people type
_THRESHOLD_RE = re.compile(r"^\d+:[0-5]?\d:[0-5]?\d(?:\.\d+)?$")
_RESULT_RE = re.compile(r"^(?:\d+:[0-5]?\d|\d+):[0-5]?\d(?:\.\d+)?$")


class MedalError(ValueError):
    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or []


def time_to_seconds(value):
    """H:MM:SS(.t) or M:SS(.t) -> seconds; None if it isn't a time (e.g. "DNS", "")."""
    if value is None:
        return None
    match = _TIME_RE.match(str(value).strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return (int(hours) if hours else 0) * 3600 + int(minutes) * 60 + float(seconds)


def valid_result_time(value):
    """True for a typed result time: H:MM:SS(.t) or MM:SS(.t)."""
    return bool(_RESULT_RE.match(str(value).strip()))


def _norm(value):
    return " ".join(str(value or "").split()).casefold()


def combo_key(distance, category, gender):
    return (_norm(distance), _norm(category), _norm(gender))


class MedalTable:
    """Lookup for one medal configuration. Matching ignores case and extra spaces."""

    def __init__(self, rows):
        self._index = {}
        for row in rows:
            key = combo_key(row["distance"], row["category"], row["gender"])
            if key not in self._index:  # first row wins, as before
                self._index[key] = [(level.title(), time_to_seconds(row.get(level))) for level in LEVELS]

    def __bool__(self):
        return bool(self._index)

    def has(self, distance, category, gender):
        return combo_key(distance, category, gender) in self._index

    def medal_for(self, distance, category, gender, seconds):
        """Gold -> Silver -> Bronze -> Finisher: the first level whose limit the time is within.

        A blank limit means that level isn't awarded.
        """
        if seconds is None:
            return ""
        for level, limit in self._index.get(combo_key(distance, category, gender), ()):
            if limit is not None and seconds <= limit:
                return level
        return ""


# ---------------------------------------------------------------- validation

def clean_row(row):
    return {f: str(row.get(f, "") or "").strip() for f in KEY_FIELDS + LEVELS}


def row_problems(row):
    problems = [f"{f.title()} is blank" for f in KEY_FIELDS if not row[f]]
    for level in LEVELS:
        value = row[level]
        if value and not _THRESHOLD_RE.match(value):
            problems.append(f"{level.title()} '{value}' is not a time like 3:45:00")
    if not any(row[level] for level in LEVELS):
        problems.append("no medal times")
    return problems


def validate_rows(rows, label="Row", first_number=1):
    problems, seen = [], {}
    for number, row in enumerate(rows, first_number):
        for problem in row_problems(row):
            problems.append(f"{label} {number}: {problem}")
        key = combo_key(row["distance"], row["category"], row["gender"])
        if all(key) and key in seen:
            problems.append(
                f"{label} {number}: duplicates {label.lower()} {seen[key]} "
                f"({row['distance']} / {row['category']} / {row['gender']})"
            )
        seen.setdefault(key, number)
    return problems


# ---------------------------------------------------------------- CSV

def decode_upload(data):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_csv(text):
    """Parse a medal CSV (Distance, Category, Gender, Gold, Silver, Bronze, Finisher).

    Raises MedalError listing every problem; nothing is imported unless the whole file is valid.
    """
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise MedalError("The file is empty")
    header = [" ".join(h.split()).casefold() for h in rows[0]]
    columns = {}
    for field in KEY_FIELDS + LEVELS:
        if field in header:
            columns[field] = header.index(field)
    missing = [f.title() for f in KEY_FIELDS if f not in columns]
    if missing or not any(level in columns for level in LEVELS):
        raise MedalError(
            "The first row must be the column headings: " + ", ".join(CSV_HEADER)
            + (f" (missing {', '.join(missing)})" if missing else "")
        )
    parsed, line_numbers = [], []
    for number, raw in enumerate(rows[1:], 2):
        if not any(cell.strip() for cell in raw):
            continue
        parsed.append(clean_row({f: raw[i] if i < len(raw) else "" for f, i in columns.items()}))
        line_numbers.append(number)
    if not parsed:
        raise MedalError("No medal rows found under the headings")
    # validate_rows numbers rows 1..n; report the real CSV line numbers instead
    problems = [_renumber(p, line_numbers) for p in validate_rows(parsed, label="Line")]
    if problems:
        raise MedalError(f"{len(problems)} problem(s) in the CSV", problems)
    return parsed


def _renumber(problem, line_numbers):
    def swap(match):
        return f"{match.group(1)} {line_numbers[int(match.group(2)) - 1]}"

    return re.sub(r"\b(Line|line) (\d+)", swap, problem)


def to_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow([row[f] for f in KEY_FIELDS + LEVELS])
    return out.getvalue()


# ---------------------------------------------------------------- saved configurations

def list_configs():
    users = {}
    for ev in db.query("SELECT id, name, medal_config_id FROM events WHERE medal_config_id IS NOT NULL"):
        users.setdefault(ev["medal_config_id"], []).append({"id": ev["id"], "name": ev["name"]})
    configs = db.query(
        "SELECT m.id, m.name, m.updated_at, "
        "(SELECT COUNT(*) FROM medal_standards s WHERE s.config_id = m.id) AS row_count "
        "FROM medal_configs m ORDER BY m.name COLLATE NOCASE"
    )
    return [
        {"id": c["id"], "name": c["name"], "updated_at": c["updated_at"],
         "rows": c["row_count"], "used_by": users.get(c["id"], [])}
        for c in configs
    ]


def get_rows(config_id):
    return [
        {f: r[f] for f in KEY_FIELDS + LEVELS}
        for r in db.query("SELECT * FROM medal_standards WHERE config_id = ? ORDER BY position", (config_id,))
    ]


def get_config(config_id):
    row = db.query_one("SELECT id, name, updated_at FROM medal_configs WHERE id = ?", (config_id,))
    if row is None:
        return None
    used_by = [dict(e) for e in db.query("SELECT id, name FROM events WHERE medal_config_id = ?", (config_id,))]
    return {"id": row["id"], "name": row["name"], "updated_at": row["updated_at"],
            "rows": get_rows(config_id), "used_by": used_by}


def load_table(config_id):
    return MedalTable(get_rows(config_id)) if config_id else None


def _check_name(name, exclude_id=None):
    name = " ".join(str(name or "").split())
    if not name:
        raise MedalError("Give the medal configuration a name")
    clash = db.query_one("SELECT id FROM medal_configs WHERE name = ? COLLATE NOCASE", (name,))
    if clash and clash["id"] != exclude_id:
        raise MedalError(f"A medal configuration called '{name}' already exists")
    return name


def _write_rows(c, config_id, rows):
    c.execute("DELETE FROM medal_standards WHERE config_id = ?", (config_id,))
    c.executemany(
        "INSERT INTO medal_standards(config_id, position, distance, category, gender, gold, silver, bronze, finisher) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(config_id, i, r["distance"], r["category"], r["gender"], r["gold"], r["silver"], r["bronze"], r["finisher"])
         for i, r in enumerate(rows)],
    )


def create_config(name, rows):
    rows = [clean_row(r) for r in rows]
    problems = validate_rows(rows)
    if problems:
        raise MedalError(f"{len(problems)} problem(s) in the medal table", problems)
    now = time.time()
    with db.transaction() as c:
        name = _check_name(name)
        cur = c.execute("INSERT INTO medal_configs(name, created_at, updated_at) VALUES(?, ?, ?)", (name, now, now))
        config_id = cur.lastrowid
        _write_rows(c, config_id, rows)
    return config_id


def update_config(config_id, name=None, rows=None):
    with db.transaction() as c:
        if c.execute("SELECT 1 FROM medal_configs WHERE id = ?", (config_id,)).fetchone() is None:
            raise MedalError("Medal configuration not found")
        if name is not None:
            c.execute("UPDATE medal_configs SET name = ? WHERE id = ?", (_check_name(name, config_id), config_id))
        if rows is not None:
            rows = [clean_row(r) for r in rows]
            problems = validate_rows(rows)
            if problems:
                raise MedalError(f"{len(problems)} problem(s) in the medal table", problems)
            _write_rows(c, config_id, rows)
        c.execute("UPDATE medal_configs SET updated_at = ? WHERE id = ?", (time.time(), config_id))


def duplicate_config(config_id, name):
    source = get_config(config_id)
    if source is None:
        raise MedalError("Medal configuration not found")
    return create_config(name, source["rows"])


def delete_config(config_id):
    users = db.query("SELECT name FROM events WHERE medal_config_id = ?", (config_id,))
    if users:
        raise MedalError("In use by: " + ", ".join(u["name"] for u in users) + ". Choose another medal configuration for those events first.")
    db.execute("DELETE FROM medal_configs WHERE id = ?", (config_id,))


def missing_combos(event_id, config_id):
    """Distance/Category/Gender combinations among an event's entrants that have no medal standard."""
    table = load_table(config_id) or MedalTable([])
    counts = {}
    for r in db.query(
        "SELECT distance, category, gender, result_secs FROM racers WHERE event_id = ? AND present = 1", (event_id,)
    ):
        if table.has(r["distance"], r["category"], r["gender"]):
            continue
        key = (r["distance"], r["category"], r["gender"])
        entry = counts.setdefault(key, {"distance": key[0], "category": key[1], "gender": key[2],
                                        "entrants": 0, "finishers": 0})
        entry["entrants"] += 1
        entry["finishers"] += r["result_secs"] is not None
    return sorted(counts.values(), key=lambda e: (e["distance"], e["gender"], e["category"]))
