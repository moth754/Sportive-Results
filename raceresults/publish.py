"""Public results page: build sections, render HTML, upload by FTP / FTPS / SFTP."""

import datetime
import ftplib
import hashlib
import io
import json
import posixpath
import re
import ssl
import threading
import time
from pathlib import Path

import jinja2

from . import db, events, settings
from .state import state

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=jinja2.select_autoescape(["html"]),
)
_lock = threading.Lock()
# FTP folder signature -> {"pages": {page file name: content hash}, "logos": logo files already on the server}
_uploaded = {}
DATA_DIR = None  # set by the app at start-up


# ---------------------------------------------------------------- sections

def public_rows(event_id, with_medals=False):
    """Finishers only, and only the fields allowed on the public page (plus the medal colour when the event uses them)."""
    rows = db.query(
        "SELECT name, distance, gender, category, result_time, result_secs, medal FROM racers "
        "WHERE event_id = ? AND present = 1 AND result_secs IS NOT NULL", (event_id,))
    return [{"name": r["name"], "distance": r["distance"], "gender": r["gender"], "category": r["category"],
             "time": r["result_time"], "secs": round(r["result_secs"], 3),
             "medal": r["medal"] if with_medals else ""} for r in rows]


def _slug(*parts):
    return re.sub(r"[^a-z0-9]+", "-", " ".join(parts).lower()).strip("-") or "section"


def _ranked(rows):
    """Positions with ties sharing a place (1, 2, 2, 4)."""
    out, previous, position = [], None, 0
    for index, row in enumerate(rows, 1):
        if row["secs"] != previous:
            position, previous = index, row["secs"]
        out.append({"pos": position, "name": row["name"], "category": row["category"],
                    "gender": row["gender"], "time": row["time"], "medal": row.get("medal", "")})
    return out


def _section(distance, title, kind, rows):
    ranked = _ranked(rows)
    return {"id": _slug(distance, title), "title": title, "kind": kind, "count": len(ranked),
            "winner": ranked[0]["name"], "winning_time": ranked[0]["time"], "rows": ranked}


def build_sections(rows, group_order):
    """Per distance: Overall, each gender, then each category within a gender, in Webscorer's order."""
    distances = []
    for d, _, _ in group_order:
        if d not in distances:
            distances.append(d)
    for row in rows:
        if row["distance"] not in distances:
            distances.append(row["distance"])

    out = []
    for distance in distances:
        finishers = sorted((r for r in rows if r["distance"] == distance),
                           key=lambda r: (r["secs"], r["name"].casefold()))
        if not finishers:
            continue
        sections = [_section(distance, "Overall", "overall", finishers)]

        genders = [g for d, g, c in group_order if d == distance and g and not c]
        genders += sorted({r["gender"] for r in finishers} - set(genders))
        for gender in genders:
            subset = [r for r in finishers if r["gender"] == gender]
            if subset:
                sections.append(_section(distance, gender or "Unspecified", "gender", subset))

        pairs = [(g, c) for d, g, c in group_order if d == distance and c]
        pairs += sorted({(r["gender"], r["category"]) for r in finishers if r["category"]} - set(pairs))
        for gender, category in pairs:
            subset = [r for r in finishers if r["gender"] == gender and r["category"] == category]
            if subset:
                sections.append(_section(distance, f"{category} · {gender}", "category", subset))

        out.append({"name": distance, "id": _slug(distance), "count": len(finishers), "sections": sections})
    return out


# ---------------------------------------------------------------- rendering

def _nice_date(iso):
    try:
        return datetime.date.fromisoformat(iso).strftime("%A %-d %B %Y")
    except (TypeError, ValueError):
        return iso or ""


def _logo_file(filename):
    if not filename or DATA_DIR is None:
        return None
    path = Path(DATA_DIR) / "logos" / filename
    return path if path.is_file() else None


def render_bundle(event_id, s=None):
    """Render the page for an event. Returns dict(html, files: {name: bytes} in upload order, hash)."""
    s = s or settings.get_all()
    event = db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    if event is None:
        raise ValueError("Event not found")
    group_order = json.loads(event["group_order"] or "[]")
    with_medals = bool(event["medal_colours"])
    distances = build_sections(public_rows(event_id, with_medals), group_order)

    logos, files = {}, {}
    for role, filename in (("organiser", event["organiser_logo"]), ("timing", s["timing_logo"])):
        path = _logo_file(filename)
        if path:
            data = path.read_bytes()
            name = f"{role}-logo-{hashlib.sha256(data).hexdigest()[:10]}.png"
            logos[role], files[name] = name, data

    context = {
        "event_name": event["name"],
        "event_date": _nice_date(event["event_date"]),
        "distances": distances,
        "finishers": sum(d["count"] for d in distances),
        "with_medals": with_medals,
        "organiser_logo": logos.get("organiser"),
        "timing_logo": logos.get("timing"),
        "timing_name": s["timing_company_name"],
        "refresh_seconds": max(60, int(s["ftp_interval_seconds"])),
    }
    digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    updated = event["data_updated_at"] or time.time()
    context["updated"] = time.strftime("%H:%M", time.localtime(updated))
    context["updated_date"] = time.strftime("%-d %b %Y", time.localtime(updated))
    html = _env.get_template("public_results.html").render(**context).encode("utf-8")
    page_name = event["page_filename"] or events.default_page_filename(event["name"], exclude_id=event_id)
    files[page_name] = html  # page last, so its logos are already there
    return {"html": html, "files": files, "hash": digest, "logos": set(logos.values()), "page_name": page_name}


# ---------------------------------------------------------------- uploading

class _FTP_TLS(ftplib.FTP_TLS):
    """FTPS that reuses the TLS session on the data connection (many servers insist on it)."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class UploadError(Exception):
    pass


def _target(s):
    keys = ("ftp_protocol", "ftp_host", "ftp_port", "ftp_user", "ftp_remote_dir")
    return "|".join(str(s[k]) for k in keys)


def _ftp_connect(s):
    port = int(s["ftp_port"]) or 21
    if s["ftp_protocol"] == "ftps":
        context = ssl.create_default_context()
        if not s["ftp_verify_tls"]:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        ftp = _FTP_TLS(context=context, timeout=25)
    else:
        ftp = ftplib.FTP(timeout=25)
    ftp.connect(s["ftp_host"], port)
    ftp.login(s["ftp_user"], s["ftp_password"])
    if s["ftp_protocol"] == "ftps":
        ftp.prot_p()
    ftp.set_pasv(bool(s["ftp_passive"]))
    remote = s["ftp_remote_dir"].strip()
    if remote:
        if remote.startswith("/"):
            ftp.cwd("/")
        for part in [p for p in remote.split("/") if p]:
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                ftp.mkd(part)
                ftp.cwd(part)
    return ftp


def _ftp_put(ftp, name, data):
    temp = name + ".uploading"
    ftp.storbinary(f"STOR {temp}", io.BytesIO(data))
    try:
        ftp.rename(temp, name)
    except ftplib.error_perm:
        try:  # some servers won't rename over an existing file
            ftp.delete(name)
            ftp.rename(temp, name)
        except ftplib.error_perm:
            ftp.storbinary(f"STOR {name}", io.BytesIO(data))
            try:
                ftp.delete(temp)
            except ftplib.all_errors:
                pass


def _sftp_session(s):
    import paramiko

    client = paramiko.SSHClient()
    known_hosts = Path(DATA_DIR or ".") / "known_hosts"
    if known_hosts.exists():
        client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # trust on first use, then pinned
    client.connect(s["ftp_host"], port=int(s["ftp_port"]) or 22, username=s["ftp_user"],
                   password=s["ftp_password"], timeout=25, allow_agent=False, look_for_keys=False)
    client.save_host_keys(str(known_hosts))
    sftp = client.open_sftp()
    remote = s["ftp_remote_dir"].strip()
    if remote:
        path = "/" if remote.startswith("/") else ""
        for part in [p for p in remote.split("/") if p]:
            path = posixpath.join(path, part) if path else part
            try:
                sftp.stat(path)
            except IOError:
                sftp.mkdir(path)
        sftp.chdir(remote)
    return client, sftp


def _sftp_put(sftp, name, data):
    temp = name + ".uploading"
    sftp.putfo(io.BytesIO(data), temp)
    try:
        sftp.posix_rename(temp, name)
    except IOError:
        try:
            sftp.remove(name)
        except IOError:
            pass
        sftp.rename(temp, name)


def upload(s, files):
    """Upload {name: bytes} in order. Raises UploadError with a readable message."""
    if not s["ftp_host"]:
        raise UploadError("FTP host isn't set")
    try:
        if s["ftp_protocol"] == "sftp":
            client, sftp = _sftp_session(s)
            try:
                for name, data in files.items():
                    _sftp_put(sftp, name, data)
            finally:
                client.close()
        else:
            ftp = _ftp_connect(s)
            try:
                for name, data in files.items():
                    _ftp_put(ftp, name, data)
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
    except UploadError:
        raise
    except Exception as exc:  # ftplib / ssl / socket / paramiko errors all end up here
        message = str(exc).strip() or type(exc).__name__
        raise UploadError(f"{type(exc).__name__}: {message}") from None


def test_connection(s):
    """Log in, open the folder and prove it's writable with a tiny temporary file."""
    probe = ".raceresults-test.txt"
    if s["ftp_protocol"] == "sftp":
        try:
            client, sftp = _sftp_session(s)
            try:
                sftp.putfo(io.BytesIO(b"test"), probe)
                sftp.remove(probe)
                where = sftp.getcwd() or s["ftp_remote_dir"] or "home folder"
            finally:
                client.close()
        except Exception as exc:
            raise UploadError(f"{type(exc).__name__}: {str(exc).strip() or 'failed'}") from None
    else:
        try:
            ftp = _ftp_connect(s)
            try:
                ftp.storbinary(f"STOR {probe}", io.BytesIO(b"test"))
                ftp.delete(probe)
                where = ftp.pwd()
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
        except Exception as exc:
            raise UploadError(f"{type(exc).__name__}: {str(exc).strip() or 'failed'}") from None
    return f"Connected to {s['ftp_host']}; folder {where} is writable"


def publish_once(force=False):
    """Render and upload the active event's page if anything changed. Returns (ok, message)."""
    s = settings.get_all()
    event_id = s["active_event_id"]
    if not event_id:
        return False, "No active event"
    if not s["ftp_host"]:
        return False, "FTP host isn't set"
    with _lock:
        state.publish["at"] = time.time()
        try:
            bundle = render_bundle(event_id, s)
        except Exception as exc:
            state.publish["error"] = f"Couldn't build the page: {exc}"
            return False, state.publish["error"]
        done = _uploaded.setdefault(_target(s), {"pages": {}, "logos": set()})
        page_name = bundle["page_name"]
        logos_needed = [n for n in bundle["logos"] if force or n not in done["logos"]]
        if not force and not logos_needed and done["pages"].get(page_name) == bundle["hash"]:
            state.publish.update(ok_at=time.time(), error=None, message=f"{page_name} is up to date")
            return True, f"No changes to {page_name} since the last upload"
        files = {n: bundle["files"][n] for n in logos_needed}
        files[page_name] = bundle["html"]
        try:
            upload(s, files)
        except UploadError as exc:
            state.publish["error"] = str(exc)
            state.activity(f"Upload of {page_name} failed: {exc}", "error")
            return False, str(exc)
        done["pages"][page_name] = bundle["hash"]
        done["logos"].update(logos_needed)
        state.publish.update(ok_at=time.time(), error=None, message=f"Uploaded {page_name}")
        state.activity(f"Uploaded {page_name}" + (f" and {len(logos_needed)} logo(s)" if logos_needed else ""))
        return True, f"Uploaded {page_name}" + (f" and {len(logos_needed)} logo(s)" if logos_needed else "")
