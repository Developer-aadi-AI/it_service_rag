/*
 * D Group chat widget - talks only to the REST API (same origin by default).
 * Embed elsewhere with: <script src=".../widget.js" data-api="https://assistant.example.com"></script>
 * All user/assistant text is rendered with textContent (no HTML injection).
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var API = (script && script.dataset.api) || "";
  var POLL_MS = 5000;
  var state = { id: null, lastSeq: 0, closed: false, sound: true, busy: false, poller: null };

  var $ = function (id) { return document.getElementById(id); };
  var log = $("vg-log"), input = $("vg-input"), statusEl = $("vg-status");

  function api(method, path, body) {
    var opts = { method: method, headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(API + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
        if (!res.ok) {
          var err = new Error((data && data.detail) || ("HTTP " + res.status));
          err.status = res.status; err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function play(kind) { if (state.sound && kind && window.vgPlay) window.vgPlay(kind); }

  function addMessage(role, text, sources) {
    var el = document.createElement("div");
    el.className = "vg-msg " + role;
    el.textContent = text;
    if (sources && sources.length) {
      var ul = document.createElement("ul");
      ul.className = "vg-sources";
      sources.forEach(function (s) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = "[" + s.number + "] " + s.title;
        li.appendChild(a); ul.appendChild(li);
      });
      el.appendChild(ul);
    }
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  function renderSession(s) {
    if (!s) return;
    state.closed = s.status === "closed";
    var text = state.closed ? "Chat ended" : (s.status === "awaiting_idle_response" ? "Waiting for you..." : "Online");
    if (!state.closed && s.attempts_remaining < s.irrelevant_limit) {
      text += " - off-topic attempts left: " + s.attempts_remaining;
    }
    statusEl.textContent = text;
    input.disabled = state.closed;
    $("vg-new").hidden = !state.closed;
    $("vg-end").disabled = state.closed;
    if (state.closed) { $("vg-contact").hidden = true; stopPolling(); }
  }

  function renderRecs(recs) {
    var box = $("vg-recs");
    box.textContent = "";
    if (!recs || !recs.primary) { box.hidden = true; return; }
    var title = document.createElement("strong");
    title.textContent = "Recommended: ";
    var a = document.createElement("a");
    a.href = recs.primary.url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = recs.primary.name;
    box.appendChild(title); box.appendChild(a);
    if (recs.related && recs.related.length) {
      box.appendChild(document.createTextNode(" - also: "));
      recs.related.forEach(function (r, i) {
        var link = document.createElement("a");
        link.href = r.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = r.name;
        if (i) box.appendChild(document.createTextNode(", "));
        box.appendChild(link);
      });
    }
    box.hidden = false;
  }

  function handleAction(action) {
    $("vg-contact").hidden = !(action && action.type === "contact_form");
    if (action && action.type === "feedback") $("vg-feedback").hidden = false;
  }

  function onMessages(messages) {
    (messages || []).forEach(function (m) {
      if (m.seq <= state.lastSeq) return;
      state.lastSeq = m.seq;
      if (m.kind === "form_submission") return;
      addMessage(m.role, m.content, m.sources);
      if (m.kind === "session_end") $("vg-feedback").hidden = false;
    });
  }

  function start() {
    log.textContent = "";
    state.lastSeq = 0;
    ["vg-feedback", "vg-contact", "vg-recs", "vg-email"].forEach(function (id) { $(id).hidden = true; });
    statusEl.textContent = "Connecting...";
    return api("POST", "/sessions", { sound_enabled: state.sound }).then(function (data) {
      state.id = data.session.session_id;
      try { sessionStorage.setItem("vg-session", state.id); } catch (e) {}
      onMessages([data.message]);
      renderSession(data.session);
      startPolling();
      input.focus();
    }).catch(function (err) {
      statusEl.textContent = err.status === 429 ? "Too many chats started - please wait a moment." : "Can't connect right now.";
    });
  }

  function resume(id) {
    state.id = id;
    return api("GET", "/sessions/" + id + "/messages?after=0").then(function (data) {
      if (data.session.status === "closed") throw new Error("closed");
      onMessages(data.messages);
      renderSession(data.session);
      startPolling();
    }).catch(function () { return start(); });
  }

  function send(text) {
    if (!text || state.busy || state.closed) return;
    state.busy = true;
    play("sent");
    addMessage("user", text);
    api("POST", "/sessions/" + state.id + "/messages", { message: text }).then(function (data) {
      state.lastSeq = Math.max(state.lastSeq, data.message.seq);
      addMessage("assistant", data.message.content, data.message.sources);
      renderRecs(data.recommendations);
      handleAction(data.action);
      renderSession(data.session);
      play(data.notification && data.notification.sound);
    }).catch(function (err) {
      if (err.status === 409) { renderSession({ status: "closed" }); addMessage("system", "This chat has ended."); }
      else if (err.status === 429) addMessage("system", "You're sending messages quickly - please wait a moment.");
      else addMessage("system", (err.data && err.data.detail) || "Sorry, something went wrong. Please try again.");
    }).then(function () { state.busy = false; });
  }

  function poll() {
    if (!state.id || state.closed) return;
    api("GET", "/sessions/" + state.id + "/messages?after=" + state.lastSeq).then(function (data) {
      if (data.messages && data.messages.length) {
        onMessages(data.messages);
        play(data.notification && data.notification.sound);
      }
      renderSession(data.session);
    }).catch(function (err) { if (err.status === 404) stopPolling(); });
  }
  function startPolling() { stopPolling(); state.poller = setInterval(poll, POLL_MS); }
  function stopPolling() { if (state.poller) clearInterval(state.poller); state.poller = null; }

  // ---- events --------------------------------------------------------------------
  $("vg-composer").addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    input.value = "";
    send(text);
  });

  $("vg-contact").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target, el = f.elements, err = $("vg-contact-error");
    // form.elements[...] (not form.name, which is the form's own name attribute)
    var body = { name: el["name"].value, email: el["email"].value, message: el["message"].value,
                 phone: el["phone"].value || null, company: el["company"].value || null };
    err.textContent = "";
    api("POST", "/sessions/" + state.id + "/contact", body).then(function (data) {
      f.reset(); f.hidden = true;
      state.lastSeq = Math.max(state.lastSeq, data.message.seq);
      addMessage("assistant", data.message.content);
      renderSession(data.session);
      play("message_received");
    }).catch(function (e2) {
      var errors = e2.data && e2.data.errors;
      err.textContent = errors ? errors.map(function (x) { return x.field + ": " + x.message; }).join("; ")
                               : (e2.message || "Could not send your details.");
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll("#vg-feedback [data-rating]"), function (btn) {
    btn.addEventListener("click", function () {
      api("POST", "/sessions/" + state.id + "/feedback", { rating: btn.dataset.rating }).then(function (data) {
        $("vg-feedback").hidden = true;
        addMessage("assistant", data.message.content);
      }).catch(function () { addMessage("system", "Could not save your feedback."); });
    });
  });

  $("vg-sound").addEventListener("click", function () {
    state.sound = !state.sound;
    if (window.vgSound) window.vgSound.setEnabled(state.sound);
    this.textContent = "Sound: " + (state.sound ? "on" : "off");
    this.setAttribute("aria-pressed", String(state.sound));
    // chain updates so rapid toggling is applied in order on the server
    var wanted = state.sound, id = state.id;
    if (id) state.prefs = (state.prefs || Promise.resolve()).then(function () {
      return api("PATCH", "/sessions/" + id + "/preferences", { sound_enabled: wanted });
    }).catch(function () {});
  });

  $("vg-transcript").addEventListener("click", function () {
    if (state.id) window.location.href = API + "/sessions/" + state.id + "/transcript";
  });

  $("vg-end").addEventListener("click", function () {
    if (!state.id || state.closed) return;
    api("POST", "/sessions/" + state.id + "/close").then(function (data) {
      state.lastSeq = Math.max(state.lastSeq, data.message.seq);
      addMessage("assistant", data.message.content);
      handleAction(data.action);
      renderSession(data.session);
      play(data.notification && data.notification.sound);
    });
  });

  $("vg-email-toggle").addEventListener("click", function () { $("vg-email").hidden = !$("vg-email").hidden; });
  $("vg-email").addEventListener("submit", function (e) {
    e.preventDefault();
    var msg = $("vg-email-msg"), email = e.target.elements["email"].value.trim();
    api("POST", "/sessions/" + state.id + "/emails", { type: "transcript", email: email || null })
      .then(function (data) { msg.textContent = data.message; })
      .catch(function (err) { msg.textContent = err.message; });
  });

  $("vg-new").addEventListener("click", function () {
    try { sessionStorage.removeItem("vg-session"); } catch (e) {}
    start();
  });

  try { state.sound = window.vgSound ? window.vgSound.isEnabled() : true; } catch (e) {}
  $("vg-sound").textContent = "Sound: " + (state.sound ? "on" : "off");
  var saved = null;
  try { saved = sessionStorage.getItem("vg-session"); } catch (e) {}
  if (saved) resume(saved); else start();
})();
