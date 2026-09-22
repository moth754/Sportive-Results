"""Flask app: the operator web page and its JSON API."""

import gzip
import logging
import os
import secrets
import time
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__, db, events, images, medals, phone, processing, publish, settings, sms, system, webscorer
from .state import state
from .workers import Workers

log = logging.getLogger("raceresults")

USER_ERRORS = (settings.SettingsError, medals.MedalError, events.EventError, processing.EditError,
               sms.SmsError, images.ImageError, publish.UploadError, webscorer.WebscorerError)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _secret_key(data_dir):
    path = data_dir / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


def _body():
    return request.get_json(silent=True) or {}


def _upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        raise ApiError("Choose a file first")
    return file.filename, file.read()


def _active_event_id(required=True):
    event_id = request.args.get("event_id", type=int) or settings.get("active_event_id")
    if required and (not event_id or events.get_event(event_id) is None):
        raise ApiError("No event selected: create or choose an event first", 404)
    return event_id


def create_app(data_dir, start_workers=True):
    data_dir = Path(data_dir)
    (data_dir / "logos").mkdir(parents=True, exist_ok=True)
    try:
        data_dir.chmod(0o700)
    except OSError:
        pass
    db_path = data_dir / "raceresults.db"
    db.init(db_path)
    try:
        db_path.chmod(0o600)
    except OSError:
        pass
    settings.reset_cache()
    events.fill_missing_page_filenames()
    publish.DATA_DIR = data_dir

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret_key(data_dir),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LENGTH=60 * 60 * 24 * 30,
    )
    app.json.sort_keys = False
    workers = Workers()
    app.extensions["raceresults_workers"] = workers

    # ------------------------------------------------------------ guards

    @app.before_request
    def guard():
        if request.endpoint in ("login", "static"):
            return None
        password_hash = settings.get("ui_password_hash")
        if password_hash and session.get("pw") != password_hash[-16:]:
            if request.path.startswith("/api/"):
                return jsonify(error="Please log in again"), 401
            return redirect(url_for("login", next=request.path))
        # Cross-site requests can't set this header, so another web page can't
        # (for example) power the computer off or switch texts on.
        if request.method not in ("GET", "HEAD", "OPTIONS") and \
                request.headers.get("X-Requested-With") != "raceresults":
            return jsonify(error="Request refused"), 403
        return None

    @app.after_request
    def finish(response):
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"  # browsers must never show a stale page after an update
        if (response.status_code == 200 and not response.direct_passthrough
                and response.mimetype in ("application/json", "text/html", "text/csv")
                and "gzip" in request.headers.get("Accept-Encoding", "")
                and "Content-Encoding" not in response.headers
                and (response.content_length or 0) > 4096):
            response.set_data(gzip.compress(response.get_data(), compresslevel=5))
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Vary"] = "Accept-Encoding"
        return response

    @app.errorhandler(ApiError)
    def api_error(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(medals.MedalError)
    def medal_error(exc):
        return jsonify(error=str(exc), problems=exc.problems), 400

    for error_class in USER_ERRORS:
        if error_class is not medals.MedalError:
            app.register_error_handler(error_class, lambda exc: (jsonify(error=str(exc)), 400))

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify(error="That file is too large (12 MB maximum)"), 413

    @app.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc, HTTPException):
            return exc
        log.exception("Unhandled error on %s", request.path)
        return jsonify(error=f"Internal error: {type(exc).__name__}"), 500

    # ------------------------------------------------------------ pages

    @app.get("/")
    def index():
        return render_template("index.html", version=__version__, boot=state.boot_id)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        password_hash = settings.get("ui_password_hash")
        if not password_hash:
            return redirect("/")
        error = None
        if request.method == "POST":
            if check_password_hash(password_hash, request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["pw"] = password_hash[-16:]
                target = request.args.get("next") or "/"
                return redirect(target if target.startswith("/") and not target.startswith("//") else "/")
            time.sleep(1)
            error = "Wrong password"
        return render_template("login.html", error=error)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/logos/<path:name>")
    def logo(name):
        return send_from_directory(data_dir / "logos", name, max_age=3600)

    @app.get("/preview/")
    @app.get("/preview/<path:name>")
    def preview(name=None):
        event_id = _active_event_id()
        bundle = publish.render_bundle(event_id)
        if name is None:
            return Response(bundle["html"], mimetype="text/html")
        if name not in bundle["files"]:
            return Response("Not found", status=404)
        return Response(bundle["files"][name], mimetype="image/png")

    # ------------------------------------------------------------ status

    @app.get("/api/status")
    def status():
        s = settings.get_all()
        event_id = s["active_event_id"]
        event = events.get_public(event_id) if event_id else None
        return jsonify(
            now=time.time(),
            stats=state.stats,
            event=event,
            version=state.version(event_id) if event else None,
            sms={"enabled": state.sms_enabled, "ready": sms.twilio_ready(s),
                 "counts": sms.counts(event_id) if event else {}},
            fetch=state.fetch.get(event_id, {}) if event else {},
            publish={"enabled": s["ftp_enabled"], "configured": bool(s["ftp_host"]), **state.publish},
            activity=state.activity_since(request.args.get("activity", 0, type=int)),
            auth=bool(s["ui_password_hash"]),
            app_version=__version__,
        )

    # ------------------------------------------------------------ events

    @app.get("/api/events")
    def list_events():
        return jsonify(events=events.list_events(), active_event_id=settings.get("active_event_id"),
                       public_url=settings.get("public_url"))

    @app.post("/api/events")
    def create_event():
        event_id = events.create_event(_body())
        return jsonify(event=events.get_public(event_id), active_event_id=settings.get("active_event_id"))

    @app.put("/api/events/<int:event_id>")
    def update_event(event_id):
        before = events.get_event(event_id)
        events.update_event(event_id, _body())
        after = events.get_event(event_id)
        if before and after and any(before[k] != after[k] for k in
                                     ("medal_config_id", "medal_colours", "info_on", "info_message")):
            processing.process_event(event_id)
        if before and after and before["race_id"] != after["race_id"]:
            state.wake_poller.set()
        if before and after and before["page_filename"] != after["page_filename"] \
                and settings.get("active_event_id") == event_id:
            state.wake_publisher.set()  # upload under the new name straight away
        return jsonify(event=events.get_public(event_id))

    @app.delete("/api/events/<int:event_id>")
    def delete_event(event_id):
        event = events.delete_event(event_id)
        images.remove_logo(data_dir, event["organiser_logo"])
        return jsonify(ok=True, active_event_id=settings.get("active_event_id"))

    @app.post("/api/events/<int:event_id>/activate")
    def activate_event(event_id):
        events.activate(event_id)
        return jsonify(ok=True, active_event_id=event_id)

    @app.post("/api/events/<int:event_id>/polling")
    def event_polling(event_id):
        events.set_polling(event_id, bool(_body().get("on")))
        return jsonify(event=events.get_public(event_id))

    @app.post("/api/events/<int:event_id>/logo")
    def event_logo(event_id):
        event = events.get_event(event_id)
        if event is None:
            raise ApiError("Event not found", 404)
        _, data = _upload()
        name = images.save_logo(data_dir, f"organiser-{event_id}", data)
        if event["organiser_logo"] != name:
            images.remove_logo(data_dir, event["organiser_logo"])
        events.set_organiser_logo(event_id, name)
        return jsonify(event=events.get_public(event_id))

    @app.delete("/api/events/<int:event_id>/logo")
    def event_logo_remove(event_id):
        event = events.get_event(event_id)
        if event is None:
            raise ApiError("Event not found", 404)
        images.remove_logo(data_dir, event["organiser_logo"])
        events.set_organiser_logo(event_id, None)
        return jsonify(event=events.get_public(event_id))

    @app.get("/api/events/<int:event_id>/export.csv")
    def export_event(event_id):
        event = events.get_event(event_id)
        if event is None:
            raise ApiError("Event not found", 404)
        filename = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in event["name"]).strip() or "results"
        return Response(processing.export_csv(event_id), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})

    @app.get("/api/webscorer/lookup")
    def webscorer_lookup():
        race_id = (request.args.get("race_id") or "").strip()
        if not race_id.isdigit():
            raise ApiError("Enter the Webscorer race ID (a number) first")
        s = settings.get_all()
        return jsonify(webscorer.lookup(race_id, s["webscorer_api_id"], s["webscorer_token"]))

    # ------------------------------------------------------------ results & edits

    @app.get("/api/results")
    def results():
        event_id = _active_event_id()
        version = state.version(event_id)
        if request.args.get("since") == version:
            return jsonify(version=version, unchanged=True)
        event = events.get_event(event_id)
        warnings, coverage = [], []
        if event["medal_colours"]:
            if not event["medal_config_id"]:
                warnings.append("Medal colours are on but this event has no medal configuration, so no medals "
                                "will show and no texts will be sent. Edit the event to choose one.")
            else:
                coverage = medals.missing_combos(event_id, event["medal_config_id"])
        return jsonify(version=version, event_id=event_id, racers=processing.results(event_id),
                       adjusted_field=event["adjusted_field"], medal_colours=bool(event["medal_colours"]),
                       warnings=warnings, coverage=coverage)

    @app.get("/api/events/<int:event_id>/racer")
    def racer(event_id):
        return jsonify(processing.racer_detail(event_id, request.args.get("key", "")))

    @app.post("/api/events/<int:event_id>/edits")
    def save_edits(event_id):
        body = _body()
        return jsonify(processing.save_edits(event_id, str(body.get("key", "")), body.get("fields") or {}))

    @app.post("/api/events/<int:event_id>/edits/revert")
    def revert_edits(event_id):
        return jsonify(processing.revert_edits(event_id, str(_body().get("key", ""))))

    @app.post("/api/events/<int:event_id>/medal-preview")
    def medal_preview(event_id):
        """Live feedback for the edit dialog: the medal the values would get, and whether the phone is textable."""
        body = _body()
        result = processing.preview_medal(event_id, body.get("distance", ""), body.get("category", ""),
                                          body.get("gender", ""), body.get("time", ""))
        number, problem = phone.normalise(body.get("phone", ""), settings.get("phone_region"))
        result["phone"] = {"ok": bool(number), "number": number or "", "error": problem or ""}
        return jsonify(result)

    # ------------------------------------------------------------ SMS

    @app.get("/api/sms/preview")
    def sms_preview():
        return jsonify(sms.preview(_active_event_id(required=False)))

    @app.post("/api/sms/enable")
    def sms_enable():
        on = bool(_body().get("on"))
        event_id = _active_event_id(required=on)
        if on and not sms.twilio_ready(settings.get_all()):
            raise ApiError("Twilio isn't set up yet: add the Account SID, Auth token and Messaging Service SID "
                           "(or From number) in Configuration")
        queued = sms.set_enabled(on, event_id)
        return jsonify(enabled=state.sms_enabled, queued=queued)

    @app.get("/api/sms/log")
    def sms_log():
        return jsonify(entries=sms.log_entries(_active_event_id()))

    @app.post("/api/sms/<int:sms_id>/resend")
    def sms_resend(sms_id):
        sms.resend(sms_id)
        return jsonify(ok=True)

    @app.post("/api/sms/test")
    def sms_test():
        number = sms.send_test(str(_body().get("to", "")))
        return jsonify(ok=True, message=f"Test text sent to {number}")

    # ------------------------------------------------------------ medal configurations

    @app.get("/api/medal-configs")
    def medal_configs():
        return jsonify(configs=medals.list_configs())

    @app.get("/api/medal-configs/<int:config_id>")
    def medal_config(config_id):
        config = medals.get_config(config_id)
        if config is None:
            raise ApiError("Medal configuration not found", 404)
        return jsonify(config)

    @app.post("/api/medal-configs")
    def medal_config_create():
        body = _body()
        config_id = medals.create_config(body.get("name"), body.get("rows") or [])
        return jsonify(medals.get_config(config_id))

    @app.put("/api/medal-configs/<int:config_id>")
    def medal_config_update(config_id):
        body = _body()
        medals.update_config(config_id, name=body.get("name"), rows=body.get("rows"))
        _reprocess_users_of(config_id)
        return jsonify(medals.get_config(config_id))

    @app.delete("/api/medal-configs/<int:config_id>")
    def medal_config_delete(config_id):
        medals.delete_config(config_id)
        return jsonify(ok=True)

    @app.post("/api/medal-configs/<int:config_id>/duplicate")
    def medal_config_duplicate(config_id):
        new_id = medals.duplicate_config(config_id, _body().get("name"))
        return jsonify(medals.get_config(new_id))

    @app.post("/api/medal-configs/import")
    def medal_config_import():
        filename, data = _upload()
        rows = medals.parse_csv(medals.decode_upload(data))
        name = request.form.get("name") or Path(filename).stem.replace("_", " ")
        config_id = medals.create_config(name, rows)
        return jsonify(medals.get_config(config_id))

    @app.get("/api/medal-configs/<int:config_id>/export.csv")
    def medal_config_export(config_id):
        config = medals.get_config(config_id)
        if config is None:
            raise ApiError("Medal configuration not found", 404)
        filename = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in config["name"]).strip() or "medals"
        return Response(medals.to_csv(config["rows"]), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})

    @app.get("/api/medal-configs/<int:config_id>/missing")
    def medal_config_missing(config_id):
        event_id = _active_event_id()
        return jsonify(missing=medals.missing_combos(event_id, config_id))

    def _reprocess_users_of(config_id):
        active = settings.get("active_event_id")
        for event in events.list_events():
            if event["medal_config_id"] == config_id and event["id"] == active:
                processing.process_event(event["id"])

    # ------------------------------------------------------------ settings

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings.public_view())

    @app.put("/api/settings")
    def put_settings():
        body = _body()
        changed = settings.update(body.get("values") or {}, clear=body.get("clear") or ())
        if changed & {"webscorer_api_id", "webscorer_token", "refresh_seconds"}:
            state.wake_poller.set()
        if any(key.startswith("ftp_") or key in ("public_url", "timing_company_name") for key in changed):
            state.wake_publisher.set()
        return jsonify(settings=settings.public_view(), changed=sorted(changed))

    @app.post("/api/settings/password")
    def set_password():
        password = str(_body().get("password") or "")
        if not password:
            settings.set_internal("ui_password_hash", "")
            session.pop("pw", None)
            state.activity("Web page password removed")
            return jsonify(ok=True, set=False)
        if len(password) < 4:
            raise ApiError("Use at least 4 characters")
        password_hash = generate_password_hash(password)
        settings.set_internal("ui_password_hash", password_hash)
        session.permanent = True
        session["pw"] = password_hash[-16:]
        state.activity("Web page password set")
        return jsonify(ok=True, set=True)

    @app.post("/api/settings/import-legacy")
    def import_legacy():
        _, data = _upload()
        imported, event_values, race_id = settings.import_legacy_config(medals.decode_upload(data))
        active = settings.get("active_event_id")
        applied_to = None
        if event_values and active and events.get_event(active) is not None:
            events.update_event(active, event_values)  # MedalColours / InfoOn / InfoMessage belong to the event
            processing.process_event(active)
            applied_to = events.get_event(active)["name"]
        state.activity(f"Imported {len(imported)} settings from an old Config.csv")
        state.wake_poller.set()
        return jsonify(imported=imported, race_id=race_id, applied_to=applied_to, settings=settings.public_view())

    @app.post("/api/settings/timing-logo")
    def timing_logo():
        _, data = _upload()
        name = images.save_logo(data_dir, "timing", data)
        old = settings.get("timing_logo")
        if old != name:
            images.remove_logo(data_dir, old)
        settings.set_internal("timing_logo", name)
        state.wake_publisher.set()
        return jsonify(settings=settings.public_view())

    @app.delete("/api/settings/timing-logo")
    def timing_logo_remove():
        images.remove_logo(data_dir, settings.get("timing_logo"))
        settings.set_internal("timing_logo", "")
        return jsonify(settings=settings.public_view())

    # ------------------------------------------------------------ FTP

    @app.post("/api/ftp/test")
    def ftp_test():
        return jsonify(ok=True, message=publish.test_connection(settings.get_all()))

    @app.post("/api/publish")
    def publish_now():
        ok, message = publish.publish_once(force=True)
        if not ok:
            raise ApiError(message)
        return jsonify(ok=True, message=message)

    # ------------------------------------------------------------ power

    @app.post("/api/shutdown")
    def shutdown():
        if not system.can_power_off():
            raise ApiError("This computer doesn't allow the app to power it off yet. "
                           "Run ./install.sh once (it adds the permission).")
        system.power_off(workers.stop)
        return jsonify(ok=True)

    if start_workers:
        workers.start()
        state.activity(f"Race Results {__version__} started; text messages are OFF")
    return app


def default_data_dir():
    return Path(os.environ.get("RACERESULTS_DATA") or Path(__file__).resolve().parent.parent / "data")
