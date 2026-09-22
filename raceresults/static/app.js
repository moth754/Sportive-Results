/* Race Results operator page. Plain JavaScript, no external libraries, so it works offline at venues. */
(() => {
  "use strict";

  // ================================================================ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;
  const fold = (s) => String(s ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const debounce = (fn, ms) => { let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); }; };
  const clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
  const rate = (bps) => bps >= 1048576 ? `${(bps / 1048576).toFixed(1)} MB/s`
    : bps >= 1024 ? `${Math.round(bps / 1024)} KB/s` : `${Math.round(bps || 0)} B/s`;
  function ago(ts, now) {
    const s = Math.max(0, Math.round(now - ts));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return clock(ts);
  }

  const MEDAL_CLASS = { Gold: "gold", Silver: "silver", Bronze: "bronze", Finisher: "finisher" };
  const medalPill = (medal) => medal ? `<span class="pill ${MEDAL_CLASS[medal] || ""}">${esc(medal)}</span>` : "";
  const SMS_LABEL = { sent: "✓ Sent", pending: "Queued", sending: "Sending", failed: "⚠ Failed",
    cancelled: "Cancelled", superseded: "Replaced" };
  const TIME_RE = /^\d+:[0-5]?\d:[0-5]?\d(\.\d+)?$/;              // medal limits: H:MM:SS
  const RESULT_RE = /^(\d+:[0-5]?\d|\d+):[0-5]?\d(\.\d+)?$/;       // result times: H:MM:SS(.t) or MM:SS

  class ApiError extends Error {
    constructor(message, problems) { super(message); this.problems = problems || []; }
  }

  async function api(method, url, body) {
    const options = { method, headers: { "X-Requested-With": "raceresults" } };
    if (body instanceof FormData) options.body = body;
    else if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(url, options);
    } catch (err) {
      throw new ApiError("Can't reach the Race Results app");
    }
    if (response.status === 401) {
      location.href = "/login?next=/";
      throw new ApiError("Please log in again");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(data.error || `Request failed (${response.status})`, data.problems);
    return data;
  }

  function toast(message, kind = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    $("#toasts").append(el);
    setTimeout(() => el.remove(), kind === "error" ? 9000 : 4500);
  }
  const fail = (err) => toast(err.message || String(err), "error");

  function confirmBox({ title, body = [], ok = "OK", danger = false, cancel = "Cancel", alert = false }) {
    const dlg = $("#dlg-confirm");
    $("#confirm-title").textContent = title;
    $("#confirm-body").replaceChildren(...[].concat(body).map((line) => {
      const p = document.createElement("p");
      p.textContent = line;
      return p;
    }));
    const okButton = $("#confirm-ok");
    okButton.textContent = ok;
    okButton.className = `btn ${danger ? "danger" : "primary"}`;
    const cancelButton = $("#confirm-cancel");
    cancelButton.textContent = cancel;
    cancelButton.hidden = alert;
    dlg.returnValue = "";
    dlg.showModal();
    (danger ? cancelButton : okButton).focus();  // dangerous actions need a deliberate click
    return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
  }

  function promptBox({ title, label, value = "", ok = "OK" }) {
    const dlg = $("#dlg-prompt");
    $("#prompt-title").textContent = title;
    $("#prompt-label").textContent = label;
    const input = $("#prompt-input");
    input.value = value;
    $("#prompt-ok").textContent = ok;
    dlg.returnValue = "";
    dlg.showModal();
    input.select();
    return new Promise((resolve) => dlg.addEventListener("close",
      () => resolve(dlg.returnValue === "ok" ? input.value.trim() : null), { once: true }));
  }

  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem(`rr.${key}`); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(`rr.${key}`, JSON.stringify(value)); } catch { /* storage unavailable */ }
    },
  };

  $$("[data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close("cancel")));

  // ================================================================ state
  const S = {
    status: null,
    activeId: null,
    events: [],
    configs: [],
    results: [],
    version: null,
    adjustedField: null,
    medalColours: false,
    warnings: [],
    coverage: [],
    coverageOpen: false,
    lastChanged: new Map(),
    firstRender: true,
    activitySince: 0,
    activity: [],
    settings: null,
    settingsDirty: false,
    clearSecrets: new Set(),
    edit: null,
    medal: { current: null, rows: [], dirty: false },
  };
  const activeEvent = () => S.events.find((e) => e.id === S.activeId) || null;
  const joinUrl = (base, file) => (base && file ? `${base.replace(/\/+$/, "")}/${file}` : "");

  // ================================================================ tabs
  function currentTab() {
    return ($(".tab[aria-selected='true']") || {}).dataset?.tab || "results";
  }

  async function canLeave(tab) {
    if (tab === "medals" && S.medal.dirty) {
      if (!(await confirmBox({ title: "Discard medal changes?", body: [`Your changes to “${S.medal.current.name}” haven't been saved.`], ok: "Discard changes", danger: true }))) return false;
      await reloadCurrentConfig();
    }
    if (tab === "config" && S.settingsDirty) {
      if (!(await confirmBox({ title: "Discard configuration changes?", body: ["Your changes on the Configuration tab haven't been saved."], ok: "Discard changes", danger: true }))) return false;
      S.settings && fillSettingsForm(S.settings);
    }
    return true;
  }

  function showTab(name) {
    if (!["results", "medals", "config"].includes(name)) name = "results";
    $$(".tab").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.tab === name)));
    $$(".tab-panel").forEach((panel) => { panel.hidden = panel.id !== `tab-${name}`; });
    store.set("tab", name);
    history.replaceState(null, "", `#${name}`);
    if (name === "medals") loadConfigs().catch(fail);
    if (name === "config") loadSettings().catch(fail);
  }

  $$(".tab").forEach((tab) => tab.addEventListener("click", async () => {
    const from = currentTab();
    if (from === tab.dataset.tab || !(await canLeave(from))) return;
    showTab(tab.dataset.tab);
  }));

  const topbar = $("#topbar");
  new ResizeObserver(() => document.documentElement.style.setProperty("--topbar-h", `${topbar.offsetHeight}px`)).observe(topbar);

  // ================================================================ header & status
  const gb = (bytes) => ((bytes || 0) / 1073741824).toFixed(1);

  function setMeter(id, pct) {
    const bar = $(`#${id}-bar`);
    bar.style.width = `${Math.min(100, pct)}%`;
    bar.className = pct >= 90 ? "crit" : pct >= 70 ? "high" : "";
    $(`#${id}`).textContent = `${Math.round(pct)}%`;
  }

  function setStatus(id, cls, text, title) {
    const el = $(id);
    el.querySelector(".dot").className = `dot ${cls}`;
    el.querySelector("b").textContent = text;
    el.title = title || text;
  }

  function renderHeader(st) {
    const stats = st.stats || {};
    if (stats.cpu !== undefined) {
      setMeter("m-cpu", stats.cpu);
      setMeter("m-ram", stats.ram);
      $("#m-ram-wrap").title = `Memory in use: ${Math.round(stats.ram_used / 1048576)} MB of ${Math.round(stats.ram_total / 1048576)} MB`;
      setMeter("m-disk", stats.disk || 0);
      $("#m-disk-wrap").title = `Storage in use: ${gb(stats.disk_used)} GB of ${gb(stats.disk_total)} GB`;
      $("#m-net").textContent = `↓ ${rate(stats.net_down)} ↑ ${rate(stats.net_up)}`;
      $("#m-temp-wrap").hidden = stats.temp == null;
      if (stats.temp != null) $("#m-temp").textContent = `${Math.round(stats.temp)}°C`;
    }
    const ev = st.event;
    $("#hdr-event").textContent = ev ? `${ev.name} · race ${ev.race_id}` : "No event selected";
    document.title = ev ? `${ev.name} · Race Results` : "Race Results";

    const on = st.sms.enabled;
    $("#sms-switch").checked = on;
    $("#sms-toggle").classList.toggle("on", on);
    $("#sms-label").textContent = on ? "SMS ON" : "SMS OFF";
    const c = st.sms.counts || {};
    const parts = [];
    if (c.sent) parts.push(`${c.sent} sent`);
    if (c.pending || c.sending) parts.push(`${(c.pending || 0) + (c.sending || 0)} queued`);
    if (c.failed) parts.push(`${c.failed} failed`);
    $("#sms-counts").textContent = parts.join(" · ");
    $("#m-sms").textContent = String(c.sent || 0) + ((c.pending || c.sending) ? ` +${(c.pending || 0) + (c.sending || 0)}` : "");
    $("#m-sms-wrap").title = parts.length ? `Texts: ${parts.join(", ")}` : "No texts sent yet for this event";
    $("#sms-panel-counts").textContent = parts.length ? `(${parts.join(", ")})` : "";

    const now = st.now;
    const f = st.fetch || {};
    if (!ev) setStatus("#st-webscorer", "", "no event");
    else if (!ev.polling) setStatus("#st-webscorer", "warn", "paused", "Live updates are paused for this event");
    else if (f.error) setStatus("#st-webscorer", "err", "error", f.error);
    else if (f.ok_at) setStatus("#st-webscorer", "ok", ago(f.ok_at, now), `Last fetched at ${clock(f.ok_at)}`);
    else setStatus("#st-webscorer", "", "waiting");

    const p = st.publish || {};
    $("#ftp-switch").checked = !!p.enabled;
    $("#ftp-toggle").classList.toggle("on", !!p.enabled);
    $("#ftp-label").textContent = p.enabled ? "FTP ON" : "FTP OFF";
    $("#ftp-counts").textContent = p.enabled ? (p.error ? "upload failed" : p.ok_at ? `uploaded ${ago(p.ok_at, now)}` : "waiting")
      : (p.configured ? "" : "not set up");
    if (!p.enabled) setStatus("#st-ftp", "", "off", "Automatic upload is off (use the FTP switch)");
    else if (p.error) setStatus("#st-ftp", "err", "error", p.error);
    else if (p.ok_at) setStatus("#st-ftp", "ok", ago(p.ok_at, now), `Checked at ${clock(p.ok_at)}: ${p.message || "OK"}`);
    else setStatus("#st-ftp", "", "waiting");
  }

  let statusTimer = null;
  let statusBusy = false;
  async function pollStatus() {
    clearTimeout(statusTimer);
    if (statusBusy) return;
    statusBusy = true;
    try {
      const st = await api("GET", `/api/status?activity=${S.activitySince}`);
      $("#offline").hidden = true;
      S.status = st;
      renderHeader(st);
      if (st.activity.length) addActivity(st.activity);
      const activeId = st.event ? st.event.id : null;
      if (activeId !== S.activeId) {
        S.activeId = activeId;
        S.version = null;
        S.firstRender = true;
        S.lastChanged.clear();
        await loadEvents();
        await loadResults();
      } else {
        if (st.event) S.events = S.events.map((e) => (e.id === st.event.id ? st.event : e));
        updateEventBar();
        renderNotices();
        if (st.version && st.version !== S.version) await loadResults();
      }
    } catch (err) {
      $("#offline").hidden = false;
    } finally {
      statusBusy = false;
      statusTimer = setTimeout(pollStatus, document.hidden ? 8000 : 2000);
    }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });

  // ================================================================ SMS switch
  $("#sms-switch").addEventListener("change", async (e) => {
    const want = e.target.checked;
    e.target.checked = !want; // unchanged until the app confirms
    try {
      if (want) {
        if (!S.activeId) { toast("Create or choose an event first", "warn"); return; }
        const p = await api("GET", "/api/sms/preview");
        if (!p.twilio_ready) { toast("Twilio isn't set up yet: add its details on the Configuration tab", "error"); return; }
        const body = [p.texts
          ? `${plural(p.texts, "text")} will be sent straight away to finishers who haven't had one yet.`
          : "Nobody is waiting for a text right now. New finishers will be texted as they arrive."];
        if (p.info) body.push(`${plural(p.info, "information message")} will follow.`);
        if (p.invalid) body.push(`${plural(p.invalid, "finisher")} with an invalid number will be skipped (listed under Text messages).`);
        body.push(p.medal_colours ? "Only finishers with a medal colour are texted."
          : "Medal colours are off, so every finisher with a phone number is texted.");
        if (!(await confirmBox({ title: "Turn text messages on?", body, ok: "Turn texts on", danger: true }))) return;
      }
      const r = await api("POST", "/api/sms/enable", { on: want });
      toast(want ? `Texts ON${r.queued ? `: ${plural(r.queued, "text")} queued` : ""}` : "Texts OFF", want ? "warn" : "ok");
      await pollStatus();
    } catch (err) {
      fail(err);
    }
  });

  $("#ftp-switch").addEventListener("change", async (e) => {
    const want = e.target.checked;
    e.target.checked = !want;
    try {
      if (want && !S.status?.publish?.configured) {
        toast("Add the FTP host, user and password on the Configuration tab first", "error");
        return;
      }
      await api("PUT", "/api/settings", { values: { ftp_enabled: want } });
      if (S.settings) S.settings.ftp_enabled = want;
      const box = $("#config-form").ftp_enabled;
      if (box) box.checked = want;
      toast(want ? "Website upload ON: the page goes up now and after every change" : "Website upload OFF");
      await pollStatus();
    } catch (err) {
      fail(err);
    }
  });

  // ================================================================ shutdown
  $("#btn-shutdown").addEventListener("click", async () => {
    const ok = await confirmBox({
      title: "Power off this computer?",
      body: ["Results, texts and uploads stop, and this page goes offline.",
        "To start again, unplug the power and plug it back in."],
      ok: "Power off", danger: true, cancel: "Keep running",
    });
    if (!ok) return;
    try {
      await api("POST", "/api/shutdown");
      clearTimeout(statusTimer);
      statusBusy = true; // stop polling
      const overlay = document.createElement("div");
      overlay.className = "overlay";
      overlay.innerHTML = "<div><h1>Powering off…</h1><p>Wait about 30 seconds, until the Pi's green light stops flashing, before unplugging it.</p></div>";
      document.body.append(overlay);
    } catch (err) {
      fail(err);
    }
  });

  // ================================================================ events
  async function loadEvents() {
    const data = await api("GET", "/api/events");
    S.events = data.events;
    S.publicUrl = data.public_url || "";
    S.activeId = data.active_event_id;
    renderEventSelect();
    updateEventBar();
  }

  function renderEventSelect() {
    const options = S.events.map((e) => `<option value="${e.id}"${e.id === S.activeId ? " selected" : ""}>${esc(e.name)}${e.event_date ? ` · ${esc(e.event_date)}` : ""}</option>`);
    if (!activeEvent()) options.unshift(`<option value="" selected>${S.events.length ? "Choose an event…" : "No events yet"}</option>`);
    $("#event-select").innerHTML = options.join("");
    const has = !!activeEvent();
    $("#btn-event-edit").disabled = !has;
    $("#btn-event-delete").disabled = !has;
    $("#polling-switch").disabled = !has;
    $("#btn-export").hidden = !has;
  }

  function updateEventBar() {
    const ev = activeEvent();
    const meta = $("#event-meta");
    if (!ev) { meta.innerHTML = ""; return; }
    $("#btn-export").href = `/api/events/${ev.id}/export.csv`;
    $("#polling-switch").checked = ev.polling;
    const bits = [
      `Webscorer race <b>${esc(ev.race_id)}</b>`,
      `Medals: <b>${ev.medal_colours ? esc(ev.medal_config_name || "no configuration!") : "off"}</b>`,
      ...(ev.info_on ? ["Info text on"] : []),
      `<b>${ev.finishers}</b> finished of <b>${ev.entrants}</b> entrants`,
    ];
    if (ev.page_filename) {
      bits.push(ev.page_url
        ? `Results page <a href="${esc(ev.page_url)}" target="_blank" rel="noopener">${esc(ev.page_filename)}</a>`
        : `Results page file <b>${esc(ev.page_filename)}</b>`);
    }
    if (ev.adjusted_field) bits.push(`Times use Webscorer's <b>${esc(ev.adjusted_field)}</b>`);
    if (ev.data_updated_at) bits.push(`Last change at ${clock(ev.data_updated_at)}`);
    meta.innerHTML = bits.map((b) => `<span>${b}</span>`).join("");
  }

  $("#event-select").addEventListener("change", async (e) => {
    const id = Number(e.target.value);
    if (!id) return;
    if (S.status?.sms.enabled && !(await confirmBox({ title: "Switch event?", body: ["Switching event turns text messages OFF. You can switch them back on afterwards."], ok: "Switch event" }))) {
      renderEventSelect();
      return;
    }
    try {
      await api("POST", `/api/events/${id}/activate`);
      await pollStatus();
    } catch (err) {
      fail(err);
      renderEventSelect();
    }
  });

  $("#polling-switch").addEventListener("change", async (e) => {
    try {
      await api("POST", `/api/events/${S.activeId}/polling`, { on: e.target.checked });
      toast(e.target.checked ? "Live updates on" : "Live updates paused");
      await loadEvents();
    } catch (err) { fail(err); }
  });

  $("#btn-event-delete").addEventListener("click", async () => {
    const ev = activeEvent();
    if (!ev) return;
    const ok = await confirmBox({
      title: `Delete “${ev.name}”?`,
      body: ["This removes the event's saved results, edits and text message log from this computer. Webscorer isn't affected.", "This can't be undone."],
      ok: "Delete event", danger: true,
    });
    if (!ok) return;
    try {
      await api("DELETE", `/api/events/${ev.id}`);
      toast("Event deleted");
      await loadEvents();
      await pollStatus();
    } catch (err) { fail(err); }
  });

  // ---------------------------------------------------------------- event dialog
  const eventDialog = { id: null, logoFile: null, removeLogo: false, pageTouched: false, medalsTouched: false };

  // Suggested page name from the event name ("Lands End 2026" -> landsend2026.html), unique among events.
  function suggestPageName(name, excludeId) {
    const base = fold(name).replace(/[^a-z0-9]/g, "").slice(0, 60) || "results";
    const taken = new Set(S.events.filter((e) => e.id !== excludeId && e.page_filename).map((e) => e.page_filename.toLowerCase()));
    let candidate = `${base}.html`;
    for (let n = 2; taken.has(candidate); n += 1) candidate = `${base}-${n}.html`;
    return candidate;
  }
  const normalisePageName = (value) => {
    const v = value.trim();
    return v && !/\.[a-z0-9]{1,5}$/i.test(v) ? `${v}.html` : v;
  };
  function renderPageHint() {
    const url = joinUrl(S.publicUrl, normalisePageName($("#event-form").page_filename.value));
    $("#page-name-hint").textContent = url ? `Live page: ${url}`
      : "This event's page on your website, uploaded to the FTP folder under this name.";
  }

  function showEventLogo(src, note = "") {
    const img = $("#event-logo");
    img.hidden = !src;
    if (src) img.src = src;
    $("#btn-event-logo-remove").hidden = !src;
    $("#event-logo-name").textContent = note;
  }

  async function openEventDialog(ev) {
    try { await loadConfigs(false); } catch { /* list stays as it was */ }
    const form = $("#event-form");
    form.reset();
    Object.assign(eventDialog, { id: ev ? ev.id : null, logoFile: null, removeLogo: false });
    $("#event-dlg-title").textContent = ev ? "Edit event" : "New event";
    form.race_id.value = ev ? ev.race_id : "";
    form.name.value = ev ? ev.name : "";
    form.event_date.value = ev ? ev.event_date : "";
    form.medal_config_id.innerHTML = `<option value="">None</option>${S.configs.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}`;
    form.medal_config_id.value = ev && ev.medal_config_id ? String(ev.medal_config_id) : "";
    form.medal_colours.checked = ev ? ev.medal_colours : false;
    form.info_on.checked = ev ? ev.info_on : false;
    form.info_message.value = ev ? ev.info_message || "" : "";
    eventDialog.medalsTouched = !!ev;
    $("#info-message-field").hidden = !form.info_on.checked;
    form.page_filename.value = ev ? ev.page_filename || "" : "";
    eventDialog.pageTouched = !!ev; // keep an existing event's web address unless it's changed by hand
    renderPageHint();
    showEventLogo(ev && ev.organiser_logo ? `/logos/${encodeURIComponent(ev.organiser_logo)}` : null);
    $("#lookup-result").textContent = "The number at the end of the race's Webscorer address (…/race?raceid=447118)";
    $("#dlg-event").showModal();
    (ev ? form.name : form.race_id).focus();
  }

  $("#btn-event-new").addEventListener("click", () => openEventDialog(null));
  $("#btn-event-edit").addEventListener("click", () => openEventDialog(activeEvent()));

  $("#event-logo-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    eventDialog.logoFile = file;
    eventDialog.removeLogo = false;
    showEventLogo(URL.createObjectURL(file), "Resized when you save");
  });
  $("#btn-event-logo-remove").addEventListener("click", () => {
    eventDialog.logoFile = null;
    eventDialog.removeLogo = true;
    $("#event-logo-file").value = "";
    showEventLogo(null);
  });

  $("#btn-lookup").addEventListener("click", async () => {
    const form = $("#event-form");
    const id = form.race_id.value.trim();
    const out = $("#lookup-result");
    if (!/^\d+$/.test(id)) { out.textContent = "Enter the race ID (a number) first"; return; }
    out.textContent = "Looking up…";
    try {
      const info = await api("GET", `/api/webscorer/lookup?race_id=${encodeURIComponent(id)}`);
      out.textContent = `Found: ${info.name || "(no name)"}${info.organiser ? ` · ${info.organiser}` : ""} · ${plural(info.racers, "racer")}`;
      if (info.name && (!eventDialog.id || !form.name.value.trim())) form.name.value = info.name;
      if (!eventDialog.pageTouched && form.name.value.trim()) form.page_filename.value = suggestPageName(form.name.value, eventDialog.id);
      renderPageHint();
      if (info.date && !form.event_date.value) form.event_date.value = info.date;
    } catch (err) {
      out.textContent = err.message;
    }
  });

  $("#event-form").addEventListener("input", (e) => {
    const form = e.currentTarget;
    if (e.target === form.page_filename) eventDialog.pageTouched = form.page_filename.value.trim() !== "";
    if (e.target === form.medal_colours) eventDialog.medalsTouched = true;
    if (e.target === form.medal_config_id && !eventDialog.medalsTouched) form.medal_colours.checked = !!form.medal_config_id.value;
    if (e.target === form.info_on) $("#info-message-field").hidden = !form.info_on.checked;
    if (e.target === form.name && !eventDialog.pageTouched) {
      form.page_filename.value = form.name.value.trim() ? suggestPageName(form.name.value, eventDialog.id) : "";
    }
    renderPageHint();
  });
  $("#event-form").page_filename.addEventListener("blur", (e) => {
    e.target.value = normalisePageName(e.target.value);
    renderPageHint();
  });

  $("#event-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = { name: form.name.value, race_id: form.race_id.value, event_date: form.event_date.value,
      medal_config_id: form.medal_config_id.value || null, page_filename: normalisePageName(form.page_filename.value),
      medal_colours: form.medal_colours.checked, info_on: form.info_on.checked, info_message: form.info_message.value };
    try {
      let id = eventDialog.id;
      let message = "Event saved";
      if (id) {
        await api("PUT", `/api/events/${id}`, body);
      } else {
        const created = await api("POST", "/api/events", body);
        id = created.event.id;
        if (created.active_event_id !== id) await api("POST", `/api/events/${id}/activate`);
        message = "Event created and selected. Text messages are off.";
      }
      if (eventDialog.logoFile) {
        const fd = new FormData();
        fd.append("file", eventDialog.logoFile);
        await api("POST", `/api/events/${id}/logo`, fd);
      } else if (eventDialog.removeLogo) {
        await api("DELETE", `/api/events/${id}/logo`);
      }
      $("#dlg-event").close();
      toast(message);
      await loadEvents();
      await pollStatus();
    } catch (err) {
      fail(err);
    }
  });

  // ================================================================ results
  async function loadResults(force = false) {
    if (!S.activeId) { renderResults(); return; }
    try {
      const since = force ? "" : (S.version || "");
      const data = await api("GET", `/api/results?since=${encodeURIComponent(since)}`);
      if (data.unchanged) return;
      Object.assign(S, {
        version: data.version, results: data.racers, adjustedField: data.adjusted_field,
        medalColours: data.medal_colours, warnings: data.warnings, coverage: data.coverage,
      });
      renderResults();
      if ($("#sms-panel").open) loadSmsLog();
    } catch (err) {
      if (!(err instanceof ApiError) || !/No event/.test(err.message)) fail(err);
    }
  }

  function renderNotices() {
    const box = $("#results-notices");
    const parts = [];
    const fetchState = S.status?.fetch;
    if (S.activeId && fetchState?.error) parts.push(`<div class="notice err"><b>Webscorer:</b> ${esc(fetchState.error)}. Showing the last results received.</div>`);
    for (const w of S.warnings) parts.push(`<div class="notice warn">${esc(w)}</div>`);
    if (S.coverage.length) {
      const total = S.coverage.reduce((n, c) => n + c.entrants, 0);
      parts.push(`<details class="notice warn" id="coverage"${S.coverageOpen ? " open" : ""}><summary><b>${plural(total, "entrant")}</b> ${total === 1 ? "is" : "are"} in ${plural(S.coverage.length, "distance / category / gender combination")} with no medal standard, so they won't get a medal colour or a text.</summary>
        <ul>${S.coverage.map((c) => `<li>${esc(c.distance)} / ${esc(c.category || "(no category)")} / ${esc(c.gender || "(no gender)")}: ${plural(c.entrants, "entrant")}${c.finishers ? `, ${c.finishers} finished` : ""}</li>`).join("")}</ul>
        <p style="margin:8px 0 0"><button class="btn small" type="button" data-action="fix-medals">Open medal setup</button></p></details>`);
    }
    const html = parts.join("");
    if (box.dataset.html !== html) {
      box.innerHTML = html;
      box.dataset.html = html;
      $("#coverage")?.addEventListener("toggle", (e) => { S.coverageOpen = e.target.open; });
    }
    box.hidden = !parts.length;
  }

  function fillFilters() {
    const distances = [...new Set(S.results.map((r) => r.distance).filter(Boolean))];
    const select = $("#filter-distance");
    const current = select.value;
    select.innerHTML = `<option value="">All distances</option>${distances.map((d) => `<option${d === current ? " selected" : ""}>${esc(d)}</option>`).join("")}`;
  }

  function columns() {
    return [
      { key: "seen", label: "Seen" },
      { key: "bib", label: "Bib" },
      { key: "name", label: "Name" },
      { key: "distance", label: "Distance" },
      { key: "category", label: "Category" },
      { key: "gender", label: "Gender" },
      { key: "time", label: "Time" },
      S.adjustedField && { key: "finish", label: "Finish (raw)" },
      S.medalColours && { key: "medal", label: "Medal" },
      S.results.some((r) => r.purchases) && { key: "purchases", label: "Purchases" },
      { key: "sms", label: "Text" },
      { key: "edit", label: "" },
    ].filter(Boolean);
  }

  function filteredRows() {
    const term = fold($("#search").value.trim());
    const distance = $("#filter-distance").value;
    const showAll = $("#show-all").checked || !!term;
    const editedOnly = $("#edited-only").checked;
    const digits = /^\d+$/.test(term);
    let rows = S.results.filter((r) => (showAll || editedOnly || r.finished) && (!distance || r.distance === distance)
      && (!editedOnly || r.edited.length));
    if (term) {
      rows = rows.filter((r) => (digits && String(r.bib).startsWith(term)) || fold(r.name).includes(term) || fold(r.bib) === term);
    }
    const byFeed = (a, b) => {
      if (a.finished !== b.finished) return a.finished ? -1 : 1;
      if (a.finished) {
        const changed = (b.changed || 0) - (a.changed || 0);
        if (changed) return changed;
        const clk = (b.clock ?? -1) - (a.clock ?? -1);
        if (clk) return clk;
      }
      return String(a.bib).localeCompare(String(b.bib), undefined, { numeric: true });
    };
    rows.sort((a, b) => (digits ? (String(b.bib) === term) - (String(a.bib) === term) : 0) || byFeed(a, b));
    return rows;
  }

  function renderCell(r, key, now) {
    const edited = r.edited.includes(key);
    const cls = [key];
    if (edited) cls.push("edited");
    const title = edited && r.orig && key in r.orig ? ` title="Edited. Webscorer: ${esc(r.orig[key] || "(blank)")}"` : "";
    const td = (content) => `<td class="${cls.join(" ")}"${title}>${content}</td>`;
    switch (key) {
      case "seen": return td(esc(clock(r.first_seen)));
      case "name": {
        let badge = "";
        if (r.finished && r.updates > 0) {
          const old = now - r.changed > 600 ? " old" : "";
          badge = `<span class="badge updated${old}" title="Result changed ${plural(r.updates, "time")}, last at ${esc(clock(r.changed))}">Updated</span>`;
        }
        if (r.edited.length) {
          badge += `<span class="badge edited" title="Edited here (${r.edited.join(", ")}). Webscorer still has the original values.">Edited</span>`;
        }
        return td(esc(r.name) + badge);
      }
      case "time": {
        let t = r.finished ? r.time : (r.time || "–");
        const tip = !edited && r.adjusted_time && r.finished ? ` title="Adjusted time (finish ${esc(r.finish_time)})"` : title;
        return `<td class="${cls.join(" ")}"${tip}>${esc(t)}</td>`;
      }
      case "finish": return td(esc(r.finish_time));
      case "medal": return td(medalPill(r.medal));
      case "purchases": return td(esc(r.purchases));
      case "sms":
        if (r.sms) return `<td><span class="sms-state ${esc(r.sms)}">${esc(SMS_LABEL[r.sms] || r.sms)}</span></td>`;
        return `<td><span class="sms-state none">${r.has_phone ? "–" : "No phone"}</span></td>`;
      case "edit": return `<td><button class="btn small" type="button" data-edit="${esc(r.key)}">Edit</button></td>`;
      default: return td(esc(r[key]));
    }
  }

  function renderResults() {
    renderNotices();
    fillFilters();
    const wrap = $("#results-wrap");
    const empty = $("#results-empty");
    if (!S.activeId) {
      wrap.hidden = true;
      empty.hidden = false;
      empty.innerHTML = S.events.length
        ? "<h2>Choose an event</h2><p>Pick an event from the list above to see its results.</p>"
        : `<h2>Create your first event</h2><p>You'll need the Webscorer race ID. Choose a medal configuration for it too if you use medal colours.</p><p><button class="btn primary" type="button" data-action="new-event">New event</button></p>`;
      $("#results-count").textContent = "";
      return;
    }
    const rows = filteredRows();
    const cols = columns();
    const now = Date.now() / 1000;
    const finishers = S.results.filter((r) => r.finished).length;
    const term = $("#search").value.trim();
    const editedCount = S.results.filter((r) => r.edited.length).length;
    $("#edited-only").parentElement.style.fontWeight = editedCount ? "700" : "";
    $("#edited-only").nextSibling.textContent = ` Edited only${editedCount ? ` (${editedCount})` : ""}`;
    $("#results-count").textContent = term || $("#filter-distance").value || $("#edited-only").checked
      ? `${plural(rows.length, "match", "matches")}`
      : `${plural(finishers, "finisher")} of ${plural(S.results.length, "entrant")}`;
    if (!rows.length) {
      wrap.hidden = true;
      empty.hidden = false;
      empty.innerHTML = term ? `<h2>No match for “${esc(term)}”</h2><p>Search covers every entrant's bib and name.</p>`
        : S.results.length ? "<h2>No finishers yet</h2><p>Finishers appear here as soon as Webscorer has their time.</p>"
          : "<h2>Waiting for Webscorer</h2><p>No entrants received yet. Check the race ID and the Webscorer settings if this doesn't change.</p>";
      return;
    }
    wrap.hidden = false;
    empty.hidden = true;
    $("#results-table thead").innerHTML = `<tr>${cols.map((c) => `<th>${esc(c.label)}</th>`).join("")}</tr>`;
    $("#results-table tbody").innerHTML = rows.map((r) => {
      const fresh = !S.firstRender && r.finished && r.changed && (S.lastChanged.get(r.key) || 0) < r.changed;
      const cls = [r.finished ? "" : "dnf", fresh ? "flash" : "", r.edited.length ? "edited" : ""].filter(Boolean).join(" ");
      return `<tr${cls ? ` class="${cls}"` : ""}>${cols.map((c) => renderCell(r, c.key, now)).join("")}</tr>`;
    }).join("");
    for (const r of S.results) S.lastChanged.set(r.key, r.changed || 0);
    S.firstRender = false;
  }

  $("#search").addEventListener("input", debounce(renderResults, 120));
  $("#search").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; renderResults(); } });
  $("#filter-distance").addEventListener("change", renderResults);
  $("#show-all").addEventListener("change", (e) => { store.set("showAll", e.target.checked); renderResults(); });
  $("#edited-only").addEventListener("change", renderResults);
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "") || $("dialog[open]");
    if (e.key === "/" && !typing && currentTab() === "results") { e.preventDefault(); $("#search").focus(); }
  });
  $("#results-table").addEventListener("click", (e) => {
    const button = e.target.closest("[data-edit]");
    if (button) openEdit(button.dataset.edit);
  });
  document.addEventListener("click", async (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action === "new-event") openEventDialog(null);
    if (action === "fix-medals") {
      const ev = activeEvent();
      if (!(await canLeave(currentTab()))) return;
      showTab("medals");
      if (ev?.medal_config_id) openConfig(ev.medal_config_id).catch(fail);
    }
  });

  // ---------------------------------------------------------------- edit dialog
  function renderOriginals() {
    const d = S.edit;
    const form = $("#edit-form");
    $$("[data-orig]", form).forEach((el) => {
      const field = el.dataset.orig;
      const original = d.original[field] || "";
      const changed = form[field].value.trim() !== original;
      let text = changed ? `Webscorer: ${original || "(blank)"}` : "";
      if (field === "time" && d.adjusted_time) {
        text = changed ? `${text} (finish ${d.finish_time}, adjusted ${d.adjusted_time})` : `Adjusted time (finish ${d.finish_time})`;
      }
      el.textContent = text;
      el.classList.toggle("changed", changed);
    });
    const time = form.time.value.trim();
    form.time.classList.toggle("invalid", !!time && !RESULT_RE.test(time));
  }

  function renderPhoneCheck(check) {
    const el = $("#edit-phone-check");
    if (!$("#edit-form").phone.value.trim()) { el.textContent = "No number: no texts"; el.style.color = ""; return; }
    el.textContent = check.ok ? `Texts go to ${check.number}` : check.error;
    el.style.color = check.ok ? "var(--ok)" : "var(--danger)";
  }

  const previewEdit = debounce(async () => {
    const form = $("#edit-form");
    try {
      const r = await api("POST", `/api/events/${S.activeId}/medal-preview`, {
        distance: form.distance.value, category: form.category.value, gender: form.gender.value,
        time: form.time.value, phone: form.phone.value,
      });
      $("#edit-medal").innerHTML = medalPill(r.medal) || '<span class="muted">none</span>';
      $("#edit-medal-note").textContent = r.note || "";
      renderPhoneCheck(r.phone);
    } catch { /* keep the last preview */ }
  }, 200);

  async function openEdit(key) {
    try {
      const d = await api("GET", `/api/events/${S.activeId}/racer?key=${encodeURIComponent(key)}`);
      S.edit = d;
      const form = $("#edit-form");
      $("#edit-bib").textContent = d.bib || "(none)";
      for (const f of ["distance", "category", "gender"]) {
        const values = [...d.options[f]];
        const current = d.current[f] || "";
        if (!values.includes(current)) values.unshift(current);
        form[f].innerHTML = values.map((v) => `<option value="${esc(v)}">${v ? esc(v) : "(blank)"}</option>`).join("");
      }
      $("#edit-options-source").textContent = d.options.source === "medals"
        ? "Distance, category and gender choices come from this event's medal configuration."
        : "Distance, category and gender choices come from the results received from Webscorer (no medal configuration on this event).";
      for (const f of ["name", "distance", "category", "gender", "time", "phone"]) form[f].value = d.current[f] || "";
      renderOriginals();
      renderPhoneCheck(d.phone_check);
      $("#edit-medal").innerHTML = medalPill(d.medal) || '<span class="muted">none</span>';
      $("#edit-medal-note").textContent = "";
      $("#btn-edit-revert").hidden = !d.edited.length;
      $("#edit-sms-warning").hidden = !S.status?.sms.enabled;
      $("#dlg-edit").showModal();
      previewEdit();
    } catch (err) {
      fail(err);
    }
  }

  $("#edit-form").addEventListener("input", () => { renderOriginals(); previewEdit(); });
  $("#edit-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const time = form.time.value.trim();
    if (time && !RESULT_RE.test(time)) { toast("Time must look like 4:26:32.8 (H:MM:SS) or 26:45 (MM:SS)", "error"); form.time.focus(); return; }
    const fields = {};
    for (const f of ["name", "distance", "category", "gender", "time", "phone"]) fields[f] = form[f].value;
    try {
      await api("POST", `/api/events/${S.activeId}/edits`, { key: S.edit.key, fields });
      $("#dlg-edit").close();
      toast(`Saved changes for bib ${S.edit.bib}`);
      await loadResults(true);
    } catch (err) {
      fail(err);
    }
  });
  $("#btn-edit-revert").addEventListener("click", async () => {
    if (!(await confirmBox({ title: "Revert to Webscorer?", body: ["All your edits for this racer are removed and Webscorer's values are used again."], ok: "Revert" }))) return;
    try {
      await api("POST", `/api/events/${S.activeId}/edits/revert`, { key: S.edit.key });
      $("#dlg-edit").close();
      toast(`Bib ${S.edit.bib} reverted to Webscorer's values`);
      await loadResults(true);
    } catch (err) { fail(err); }
  });

  // ---------------------------------------------------------------- SMS log & activity
  async function loadSmsLog() {
    const tbody = $("#sms-table tbody");
    if (!S.activeId) { tbody.innerHTML = '<tr><td colspan="6" class="muted">No event selected.</td></tr>'; return; }
    try {
      const { entries } = await api("GET", "/api/sms/log");
      tbody.innerHTML = entries.length ? entries.map((e) => `<tr>
        <td class="nowrap mono">${esc(clock(e.updated_at))}</td>
        <td class="bib">${esc(e.bib)}</td>
        <td class="nowrap mono">${esc(e.to_number || e.raw_number || "–")}</td>
        <td>${esc(e.message)}${e.kind === "info" ? ' <span class="muted small">(info)</span>' : ""}</td>
        <td><span class="sms-state ${esc(e.status)}">${esc(SMS_LABEL[e.status] || e.status)}</span>${e.error ? `<div class="small" style="color:var(--danger)">${esc(e.error)}</div>` : ""}</td>
        <td>${["failed", "cancelled", "sent", "sending"].includes(e.status) ? `<button class="btn small" type="button" data-resend="${e.id}" data-status="${esc(e.status)}">Resend</button>` : ""}</td>
      </tr>`).join("") : '<tr><td colspan="6" class="muted">No texts for this event yet.</td></tr>';
    } catch (err) { fail(err); }
  }
  $("#sms-panel").addEventListener("toggle", () => { if ($("#sms-panel").open) loadSmsLog(); });
  $("#sms-table").addEventListener("click", async (e) => {
    const button = e.target.closest("[data-resend]");
    if (!button) return;
    if (button.dataset.status === "sent" && !(await confirmBox({ title: "Send this text again?", body: ["This text has already been sent. Send another copy to the racer's current number?"], ok: "Send again" }))) return;
    try {
      await api("POST", `/api/sms/${button.dataset.resend}/resend`);
      toast("Text queued");
      loadSmsLog();
    } catch (err) { fail(err); }
  });

  function addActivity(items) {
    S.activity.push(...items);
    S.activity = S.activity.slice(-200);
    S.activitySince = items[items.length - 1].id;
    $("#activity").innerHTML = S.activity.slice().reverse()
      .map((a) => `<li class="${esc(a.level)}"><time>${esc(clock(a.at))}</time><span>${esc(a.message)}</span></li>`).join("");
  }

  // ================================================================ medal setup
  async function loadConfigs(render = true) {
    const data = await api("GET", "/api/medal-configs");
    S.configs = data.configs;
    if (render) {
      renderConfigList();
      if (!S.medal.current) {
        const remembered = store.get("medalConfig", null);
        if (remembered && S.configs.some((c) => c.id === remembered)) await openConfig(remembered);
      }
    }
  }

  function renderConfigList() {
    const list = $("#config-list");
    list.innerHTML = S.configs.length ? S.configs.map((c) => `<li><button type="button" data-config="${c.id}" aria-current="${S.medal.current?.id === c.id}">
        <b>${esc(c.name)}</b><span>${plural(c.rows, "row")}${c.used_by.length ? ` · used by ${c.used_by.map((u) => esc(u.name)).join(", ")}` : ""}</span></button></li>`).join("")
      : '<li class="muted small" style="padding:8px 10px">None yet</li>';
    const has = !!S.medal.current;
    $("#config-editor").hidden = !has;
    $("#config-empty").hidden = has;
  }

  function setMedalDirty(dirty) {
    S.medal.dirty = dirty;
    $("#config-dirty").hidden = !dirty;
  }

  async function openConfig(id) {
    if (S.medal.dirty && S.medal.current?.id !== id && !(await confirmBox({ title: "Discard medal changes?", body: [`Your changes to “${S.medal.current.name}” haven't been saved.`], ok: "Discard changes", danger: true }))) return;
    const cfg = await api("GET", `/api/medal-configs/${id}`);
    S.medal = { current: cfg, rows: cfg.rows.map((r) => ({ ...r })), dirty: false };
    store.set("medalConfig", id);
    $("#config-filter").value = "";
    renderConfigList();
    renderConfigEditor();
  }

  async function reloadCurrentConfig() {
    if (S.medal.current) {
      S.medal.dirty = false;
      await openConfig(S.medal.current.id);
    }
  }

  function renderConfigEditor() {
    const cfg = S.medal.current;
    $("#config-name").value = cfg.name;
    $("#btn-config-export").href = `/api/medal-configs/${cfg.id}/export.csv`;
    const active = cfg.used_by.find((u) => u.id === S.activeId);
    const banner = $("#config-banner");
    banner.hidden = !active;
    banner.innerHTML = active ? `<div class="notice warn">Used by the active event, <b>${esc(active.name)}</b>. Saving recalculates its medals straight away; if texts are on, anyone whose medal changes is sent a corrected text.</div>` : "";
    $("#config-problems").hidden = true;
    renderGrid();
    setMedalDirty(S.medal.dirty);
  }

  const MEDAL_FIELDS = ["distance", "category", "gender", "gold", "silver", "bronze", "finisher"];
  const isTimeField = (f) => !["distance", "category", "gender"].includes(f);
  const cellInvalid = (f, v) => (isTimeField(f) ? !!v.trim() && !TIME_RE.test(v.trim()) : !v.trim());

  function renderGrid() {
    const term = fold($("#config-filter").value.trim());
    $("#config-grid tbody").innerHTML = S.medal.rows.map((row, i) => {
      const visible = !term || fold(`${row.distance} ${row.category} ${row.gender}`).includes(term);
      const cells = MEDAL_FIELDS.map((f) => `<td><input data-field="${f}" value="${esc(row[f])}" class="${isTimeField(f) ? "time" : ""}${cellInvalid(f, row[f]) ? " invalid" : ""}" aria-label="${f}"${isTimeField(f) ? ' placeholder="H:MM:SS"' : ""}></td>`).join("");
      return `<tr data-row="${i}"${visible ? "" : " hidden"}>${cells}<td><button class="btn link small" type="button" data-remove="${i}" title="Remove this row" aria-label="Remove row">✕</button></td></tr>`;
    }).join("");
    $("#config-rowcount").textContent = plural(S.medal.rows.length, "row");
  }

  $("#config-list").addEventListener("click", (e) => {
    const button = e.target.closest("[data-config]");
    if (button) openConfig(Number(button.dataset.config)).catch(fail);
  });
  $("#config-filter").addEventListener("input", debounce(renderGrid, 120));
  $("#config-name").addEventListener("input", () => setMedalDirty(true));
  $("#config-grid").addEventListener("input", (e) => {
    const input = e.target.closest("input[data-field]");
    if (!input) return;
    const field = input.dataset.field;
    S.medal.rows[Number(input.closest("tr").dataset.row)][field] = input.value;
    input.classList.toggle("invalid", cellInvalid(field, input.value));
    setMedalDirty(true);
  });
  $("#config-grid").addEventListener("click", (e) => {
    const button = e.target.closest("[data-remove]");
    if (!button) return;
    S.medal.rows.splice(Number(button.dataset.remove), 1);
    renderGrid();
    setMedalDirty(true);
  });

  $("#btn-config-add").addEventListener("click", () => {
    const last = S.medal.rows[S.medal.rows.length - 1];
    S.medal.rows.push({ distance: last?.distance || "", category: "", gender: last?.gender || "", gold: "", silver: "", bronze: "", finisher: "" });
    $("#config-filter").value = "";
    renderGrid();
    setMedalDirty(true);
    const input = $("#config-grid tbody tr:last-child input[data-field='category']");
    input?.scrollIntoView({ block: "nearest" });
    input?.focus();
  });

  $("#btn-config-missing").addEventListener("click", async () => {
    if (!S.activeId) { toast("Choose an event on the Results tab first", "warn"); return; }
    try {
      const { missing } = await api("GET", `/api/medal-configs/${S.medal.current.id}/missing`);
      const key = (r) => [r.distance, r.category, r.gender].map(fold).map((s) => s.trim()).join("|");
      const have = new Set(S.medal.rows.map(key));
      const add = missing.filter((m) => !have.has(key(m)));
      if (!add.length) { toast("Every distance / category / gender in the active event already has a row"); return; }
      for (const m of add) S.medal.rows.push({ distance: m.distance, category: m.category, gender: m.gender, gold: "", silver: "", bronze: "", finisher: "" });
      $("#config-filter").value = "";
      renderGrid();
      setMedalDirty(true);
      toast(`Added ${plural(add.length, "row")}: fill in the times, then Save`);
    } catch (err) { fail(err); }
  });

  function showProblems(err) {
    const list = $("#config-problems");
    if (err.problems?.length) {
      list.innerHTML = err.problems.slice(0, 60).map((p) => `<li>${esc(p)}</li>`).join("")
        + (err.problems.length > 60 ? `<li>…and ${err.problems.length - 60} more</li>` : "");
      list.hidden = false;
    }
    fail(err);
  }

  $("#btn-config-save").addEventListener("click", async () => {
    try {
      const saved = await api("PUT", `/api/medal-configs/${S.medal.current.id}`, { name: $("#config-name").value, rows: S.medal.rows });
      S.medal = { current: saved, rows: saved.rows.map((r) => ({ ...r })), dirty: false };
      await loadConfigs(false);
      renderConfigList();
      renderConfigEditor();
      toast("Medal configuration saved");
    } catch (err) {
      showProblems(err);
    }
  });

  $("#btn-config-new").addEventListener("click", async () => {
    if (S.medal.dirty && !(await canLeave("medals"))) return;
    const name = await promptBox({ title: "New medal configuration", label: "Name", ok: "Create" });
    if (!name) return;
    try {
      const created = await api("POST", "/api/medal-configs", { name, rows: [] });
      await loadConfigs(false);
      await openConfig(created.id);
      $("#btn-config-add").click();
    } catch (err) { fail(err); }
  });

  $("#config-import").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (S.medal.dirty && !(await canLeave("medals"))) return;
    const name = await promptBox({ title: "Import medal CSV", label: "Save it as", value: file.name.replace(/\.csv$/i, "").replace(/_/g, " "), ok: "Import" });
    if (!name) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", name);
    try {
      const created = await api("POST", "/api/medal-configs/import", fd);
      await loadConfigs(false);
      await openConfig(created.id);
      toast(`Imported ${plural(created.rows.length, "row")} as “${created.name}”`);
    } catch (err) {
      if (err.problems?.length) {
        await confirmBox({ title: "The CSV wasn't imported", body: [err.message, ...err.problems.slice(0, 15), ...(err.problems.length > 15 ? [`…and ${err.problems.length - 15} more`] : [])], ok: "Close", alert: true });
      } else fail(err);
    }
  });

  $("#btn-config-duplicate").addEventListener("click", async () => {
    const cfg = S.medal.current;
    if (S.medal.dirty && !(await canLeave("medals"))) return;
    const name = await promptBox({ title: "Duplicate medal configuration", label: "Name for the copy", value: `${cfg.name} (copy)`, ok: "Duplicate" });
    if (!name) return;
    try {
      const created = await api("POST", `/api/medal-configs/${cfg.id}/duplicate`, { name });
      await loadConfigs(false);
      await openConfig(created.id);
      toast("Copy created");
    } catch (err) { fail(err); }
  });

  $("#btn-config-delete").addEventListener("click", async () => {
    const cfg = S.medal.current;
    if (!(await confirmBox({ title: `Delete “${cfg.name}”?`, body: ["The medal configuration is removed from this computer. Export it first if you might want it again."], ok: "Delete", danger: true }))) return;
    try {
      await api("DELETE", `/api/medal-configs/${cfg.id}`);
      S.medal = { current: null, rows: [], dirty: false };
      store.set("medalConfig", null);
      await loadConfigs();
      toast("Medal configuration deleted");
    } catch (err) { fail(err); }
  });

  // ================================================================ configuration
  function setSettingsDirty(dirty) {
    S.settingsDirty = dirty;
    $("#settings-dirty").hidden = !dirty;
  }

  function fillSecrets(s) {
    const form = $("#config-form");
    $$(".secret-state", form).forEach((box) => {
      const key = box.dataset.for;
      const set = s[`${key}_set`];
      form[key].value = "";
      form[key].placeholder = set ? "••••••••  (saved)" : "";
      box.innerHTML = set ? '<span class="set">✓ Saved</span><span class="muted">Leave blank to keep it.</span>'
        + `<button type="button" class="btn link small" data-clear="${key}">Remove</button>` : '<span class="muted">Not set</span>';
    });
  }

  function fillLogo(s) {
    const img = $("#timing-logo");
    img.hidden = !s.timing_logo;
    if (s.timing_logo) img.src = `/logos/${encodeURIComponent(s.timing_logo)}`;
    $("#btn-timing-logo-remove").hidden = !s.timing_logo;
  }

  function fillPassword(s) {
    $("#password-state").textContent = s.ui_password_set
      ? "A password is set. New devices are asked for it."
      : "No password: anyone on this network can open this page. Set one if the network is shared.";
    $("#btn-password-remove").hidden = !s.ui_password_set;
    $("#btn-logout").hidden = !s.ui_password_set;
  }

  function protocolChanged() {
    const protocol = $("#config-form").ftp_protocol.value;
    $("#ftp-passive-wrap").hidden = protocol === "sftp";
    $("#ftp-verify-wrap").hidden = protocol !== "ftps";
    $("#ftp-port-hint").textContent = `Blank or 0 = ${protocol === "sftp" ? 22 : 21}`;
  }

  function fillSettingsForm(s) {
    const form = $("#config-form");
    for (const el of form.elements) {
      if (!el.name || el.hasAttribute("data-secret") || !(el.name in s)) continue;
      if (el.type === "checkbox") el.checked = !!s[el.name];
      else el.value = s[el.name] ?? "";
    }
    if (!s.ftp_port) form.ftp_port.value = "";
    S.clearSecrets = new Set();
    fillSecrets(s);
    fillLogo(s);
    fillPassword(s);
    protocolChanged();
    const link = $("#public-link");
    const liveUrl = joinUrl(s.public_url, activeEvent()?.page_filename);
    link.hidden = !liveUrl;
    if (liveUrl) link.href = liveUrl;
    setSettingsDirty(false);
  }

  async function loadSettings() {
    if (S.settingsDirty) return; // keep unsaved typing
    S.settings = await api("GET", "/api/settings");
    fillSettingsForm(S.settings);
  }

  function collectSettings() {
    const values = {};
    for (const el of $("#config-form").elements) {
      if (!el.name) continue;
      if (el.hasAttribute("data-secret")) {
        if (el.value.trim()) values[el.name] = el.value;
      } else {
        values[el.name] = el.type === "checkbox" ? el.checked : el.value;
      }
    }
    if (values.ftp_port === "") values.ftp_port = 0;
    return { values, clear: [...S.clearSecrets] };
  }

  async function saveSettings(quiet = false) {
    const saved = await api("PUT", "/api/settings", collectSettings());
    S.settings = saved.settings;
    fillSettingsForm(saved.settings);
    if (!quiet) toast(saved.changed.length ? "Configuration saved" : "Nothing had changed");
  }

  $("#config-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await saveSettings(); } catch (err) { fail(err); }
  });
  $("#config-form").addEventListener("input", (e) => { if (e.target.name) setSettingsDirty(true); });
  $("#config-form").addEventListener("change", (e) => {
    if (e.target.name === "ftp_protocol") protocolChanged();
    if (e.target.name) setSettingsDirty(true);
  });
  $("#config-form").addEventListener("click", (e) => {
    const button = e.target.closest("[data-clear]");
    if (!button) return;
    S.clearSecrets.add(button.dataset.clear);
    button.parentElement.innerHTML = '<span class="dirty">Will be removed when you save</span>';
    setSettingsDirty(true);
  });

  async function withSaved(button, action) {
    button.disabled = true;
    try {
      if (S.settingsDirty) await saveSettings(true);
      await action();
    } catch (err) {
      fail(err);
    } finally {
      button.disabled = false;
    }
  }
  $("#btn-sms-test").addEventListener("click", (e) => withSaved(e.currentTarget, async () => {
    const r = await api("POST", "/api/sms/test", { to: $("#sms-test-to").value });
    toast(r.message);
  }));
  $("#btn-ftp-test").addEventListener("click", (e) => withSaved(e.currentTarget, async () => {
    toast("Connecting…", "warn");
    const r = await api("POST", "/api/ftp/test");
    toast(r.message);
  }));
  $("#btn-publish").addEventListener("click", (e) => withSaved(e.currentTarget, async () => {
    const r = await api("POST", "/api/publish");
    toast(r.message);
  }));
  $("#btn-preview").addEventListener("click", (e) => {
    if (!S.activeId) { e.preventDefault(); toast("Choose an event on the Results tab first", "warn"); }
  });

  $("#timing-logo-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await api("POST", "/api/settings/timing-logo", fd);
      fillLogo(r.settings);
      toast("Logo saved");
    } catch (err) { fail(err); }
  });
  $("#btn-timing-logo-remove").addEventListener("click", async () => {
    try {
      const r = await api("DELETE", "/api/settings/timing-logo");
      fillLogo(r.settings);
      toast("Logo removed");
    } catch (err) { fail(err); }
  });

  $("#btn-password-set").addEventListener("click", async () => {
    const input = $("#password-new");
    if (input.value.length < 4) { toast("Use at least 4 characters", "warn"); return; }
    try {
      await api("POST", "/api/settings/password", { password: input.value });
      input.value = "";
      fillPassword(await api("GET", "/api/settings"));
      toast("Password set");
    } catch (err) { fail(err); }
  });
  $("#btn-password-remove").addEventListener("click", async () => {
    if (!(await confirmBox({ title: "Remove the password?", body: ["Anyone on this network will be able to open this page."], ok: "Remove password", danger: true }))) return;
    try {
      await api("POST", "/api/settings/password", { password: "" });
      fillPassword(await api("GET", "/api/settings"));
      toast("Password removed");
    } catch (err) { fail(err); }
  });

  $("#legacy-import").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await api("POST", "/api/settings/import-legacy", fd);
      S.settings = r.settings;
      fillSettingsForm(r.settings);
      toast(`Imported ${plural(r.imported.length, "setting")} from ${file.name}${r.applied_to ? `; medal/info settings applied to ${r.applied_to}` : ""}`);
      if (r.applied_to) loadEvents().catch(fail);
      if (r.race_id && !S.events.some((ev) => ev.race_id === r.race_id)
        && await confirmBox({ title: "Create an event?", body: [`The file also names Webscorer race ${r.race_id}. Create an event for it?`], ok: "Create event" })) {
        showTab("results");
        await openEventDialog(null);
        $("#event-form").race_id.value = r.race_id;
        $("#btn-lookup").click();
      }
    } catch (err) { fail(err); }
  });

  window.addEventListener("beforeunload", (e) => {
    if (S.settingsDirty || S.medal.dirty) e.preventDefault();
  });

  // ================================================================ start
  async function init() {
    $("#show-all").checked = store.get("showAll", false);
    showTab(location.hash.slice(1) || store.get("tab", "results"));
    try { await loadEvents(); } catch (err) { fail(err); }
    await loadResults();
    pollStatus();
  }
  init();
})();
