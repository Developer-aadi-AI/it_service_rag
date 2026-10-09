/*
 * D Group chat notification sounds (frontend only; no audio files needed).
 *
 * Usage:
 *   vgPlay("sent")              - user sent a message (play locally, on send)
 *   vgPlay(notification.sound)  - value from the API response "notification.sound":
 *                                 "message_received" | "idle_check" | "session_ended" | null
 *   vgSound.setEnabled(false)   - mute; also PATCH /sessions/{id}/preferences {"sound_enabled": false}
 *                                 so the API stops returning sound hints for this chat.
 *
 * Browsers only allow audio after a user gesture; the first click/keypress unlocks it.
 */
(function () {
  var enabled = true;
  try { enabled = localStorage.getItem("vg-sound") !== "off"; } catch (e) {}
  var ctx = null;
  var TONES = {
    sent: [[660, 0.06]],
    message_received: [[880, 0.08], [1175, 0.10]],
    idle_check: [[523, 0.12], [523, 0.12]],
    session_ended: [[784, 0.10], [523, 0.16]]
  };

  function context() {
    if (!ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }

  window.vgPlay = function (kind) {
    if (!enabled || !kind) return;
    try {
      var c = context();
      if (!c) return;
      var t = c.currentTime;
      (TONES[kind] || TONES.message_received).forEach(function (tone) {
        var osc = c.createOscillator(), gain = c.createGain();
        osc.type = "sine";
        osc.frequency.value = tone[0];
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.15, t + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + tone[1]);
        osc.connect(gain);
        gain.connect(c.destination);
        osc.start(t);
        osc.stop(t + tone[1] + 0.02);
        t += tone[1] + 0.04;
      });
    } catch (e) { /* sound is best-effort */ }
  };

  window.vgSound = {
    setEnabled: function (on) {
      enabled = !!on;
      try { localStorage.setItem("vg-sound", enabled ? "on" : "off"); } catch (e) {}
    },
    isEnabled: function () { return enabled; }
  };

  ["click", "keydown"].forEach(function (evt) {
    window.addEventListener(evt, function () { context(); }, { once: true });
  });
})();
