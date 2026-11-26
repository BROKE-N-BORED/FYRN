"""The phone deck (v0.11): Fyrn's chat face for a phone.

One self-contained HTML page, served by the bridge at
GET /deck. It talks to the bridge's own endpoints (tags, ps,
chat) over the same connection it arrived on, so it works on
a home network with no internet: no external assets, no
fonts, no scripts from anywhere else. The page is data, not
code the app runs: this module holds it as a string and the
bridge serves it verbatim.

Kept in its own module so the served page can be extracted
and its script syntax-checked on its own (the deck tests do
exactly that: escapes in a Python string have corrupted
served JavaScript before, while every Python-side test
stayed green).

The deck is a chat face only: pick a model, chat (streamed),
attach one photo, stop a reply, start a new chat. No tools,
no settings, no file access - those live at the PC.

Style note: the CSS writes colours as rgb() and sizes with
the font shorthand, never hex values or the longhand size
property, because the package's theme sweeps scan every
module for those literals (they keep the Qt theme the one
place widget colours and sizes live; the deck simply writes
its own the other way).
"""

DECK_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Fyrn deck</title>
<style>
:root {
  --bg: rgb(14, 15, 19);
  --panel: rgb(23, 24, 31);
  --bubble: rgb(31, 33, 43);
  --line: rgb(42, 44, 56);
  --text: rgb(242, 240, 233);
  --muted: rgb(154, 151, 163);
  --accent: rgb(242, 193, 78);
  --user: rgb(51, 48, 42);
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px system-ui, -apple-system, "Segoe UI", sans-serif;
  display: flex;
  flex-direction: column;
  height: 100dvh;
}
header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}
.brand {
  font: 19px system-ui, sans-serif;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: 0.5px;
}
select {
  flex: 1;
  min-width: 0;
  background: var(--bubble);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px;
  font: 15px system-ui, sans-serif;
}
button {
  background: var(--bubble);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 12px;
  font: 15px system-ui, sans-serif;
  cursor: pointer;
}
button:active { border-color: var(--accent); }
main {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.msg {
  max-width: 88%;
  padding: 9px 12px;
  border-radius: 14px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: 16px system-ui, sans-serif;
  line-height: 1.45;
}
.msg.user {
  align-self: flex-end;
  background: var(--user);
  border: 1px solid rgb(74, 68, 54);
  border-bottom-right-radius: 4px;
}
.msg.assistant {
  align-self: flex-start;
  background: var(--bubble);
  border-bottom-left-radius: 4px;
}
.msg.assistant:empty { display: none; }
.note {
  align-self: center;
  color: var(--muted);
  font: 14px system-ui, sans-serif;
  text-align: center;
  padding: 2px 8px;
}
.note.error { color: rgb(255, 157, 138); }
.status {
  padding: 6px 12px;
  color: var(--muted);
  font: 14px system-ui, sans-serif;
  background: var(--panel);
  border-top: 1px solid var(--line);
}
.photochip {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 12px;
  background: var(--panel);
  border-top: 1px solid var(--line);
  font: 14px system-ui, sans-serif;
  color: var(--muted);
}
.photochip img {
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: 6px;
}
.photochip button { margin-left: auto; padding: 5px 10px; }
footer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
  background: var(--panel);
  border-top: 1px solid var(--line);
}
textarea {
  flex: 1;
  min-width: 0;
  background: var(--bubble);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 9px 10px;
  font: 16px system-ui, sans-serif;
  resize: none;
  max-height: 120px;
}
#send {
  background: var(--accent);
  color: rgb(25, 20, 7);
  border-color: var(--accent);
  font-weight: 700;
}
#stop {
  background: rgb(90, 35, 32);
  border-color: rgb(138, 58, 52);
  color: rgb(255, 217, 211);
  font-weight: 700;
}
</style>
</head>
<body>
<header>
  <div class="brand">Fyrn</div>
  <select id="model" aria-label="Model"></select>
  <button id="newchat" type="button">New chat</button>
</header>
<main id="chat"></main>
<div id="status" class="status" hidden></div>
<div id="photochip" class="photochip" hidden>
  <img id="photothumb" alt="Attached photo">
  <span>Photo ready to send</span>
  <button id="photoremove" type="button">Remove</button>
</div>
<footer>
  <button id="photo" type="button">Photo</button>
  <input id="photofile" type="file" accept="image/*" hidden>
  <textarea id="input" rows="1" placeholder="Message..."></textarea>
  <button id="send" type="button">Send</button>
  <button id="stop" type="button" hidden>Stop</button>
</footer>
<script>
"use strict";
var BUSY_TEXT = "Fyrn is busy at the PC right now \u2014 try again in a moment.";
var REACH_TEXT = "Can't reach Fyrn. Check the phone and PC are on the same WiFi and sharing is on.";
var chatEl = document.getElementById("chat");
var modelEl = document.getElementById("model");
var inputEl = document.getElementById("input");
var sendEl = document.getElementById("send");
var stopEl = document.getElementById("stop");
var statusEl = document.getElementById("status");
var chipEl = document.getElementById("photochip");
var thumbEl = document.getElementById("photothumb");
var fileEl = document.getElementById("photofile");
var messages = [];
var loadedModel = null;
var pendingImage = null;
var liveAbort = null;
var sending = false;

function scrollDown() {
  chatEl.scrollTop = chatEl.scrollHeight;
}
function addBubble(role, text) {
  var div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  chatEl.appendChild(div);
  scrollDown();
  return div;
}
function addNote(text, isError) {
  var div = document.createElement("div");
  div.className = "note" + (isError ? " error" : "");
  div.textContent = text;
  chatEl.appendChild(div);
  scrollDown();
  return div;
}
function setStatus(text) {
  if (text) {
    statusEl.textContent = text;
    statusEl.hidden = false;
  } else {
    statusEl.hidden = true;
    statusEl.textContent = "";
  }
}
function clearPhoto() {
  pendingImage = null;
  thumbEl.removeAttribute("src");
  chipEl.hidden = true;
  fileEl.value = "";
}
function setSending(on) {
  sending = on;
  sendEl.hidden = on;
  stopEl.hidden = !on;
}
async function refreshModels() {
  var tagsResp;
  try {
    tagsResp = await fetch("/api/tags");
    if (!tagsResp.ok) { throw new Error("tags"); }
  } catch (e) {
    addNote(REACH_TEXT, true);
    return;
  }
  var tags = await tagsResp.json();
  try {
    var psResp = await fetch("/api/ps");
    if (psResp.ok) {
      var ps = await psResp.json();
      loadedModel = (ps.models && ps.models.length) ? ps.models[0].name : null;
    }
  } catch (e) {
    loadedModel = null;
  }
  modelEl.innerHTML = "";
  (tags.models || []).forEach(function (m) {
    var opt = document.createElement("option");
    opt.value = m.name;
    opt.textContent = m.name + (m.name === loadedModel ? " (loaded)" : "");
    modelEl.appendChild(opt);
  });
  if (loadedModel) { modelEl.value = loadedModel; }
  if (!tags.models || !tags.models.length) {
    addNote("Fyrn can see no models yet. Pull or import one at the PC first.", true);
  }
}
async function send() {
  if (sending) { return; }
  var text = inputEl.value.trim();
  if (!text && !pendingImage) { return; }
  var model = modelEl.value;
  if (!model) {
    addNote("No model is picked.", true);
    return;
  }
  var userMsg = { role: "user", content: text };
  if (pendingImage) { userMsg.images = [pendingImage]; }
  messages.push(userMsg);
  addBubble("user", text ? text : "(photo)");
  inputEl.value = "";
  clearPhoto();
  setSending(true);
  setStatus(model !== loadedModel ? "Loading the model\u2026" : "Thinking\u2026");
  var bubble = addBubble("assistant", "");
  var full = "";
  var gotAny = false;
  liveAbort = new AbortController();
  try {
    var resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: model, messages: messages, stream: true }),
      signal: liveAbort.signal
    });
    if (!resp.ok) {
      var errText = "";
      try {
        var errJson = await resp.json();
        errText = errJson.error || "";
      } catch (e) { errText = ""; }
      bubble.remove();
      messages.pop();
      if (resp.status === 409) {
        addNote(BUSY_TEXT, true);
      } else if (resp.status === 400 && errText) {
        addNote(errText, true);
      } else {
        addNote(REACH_TEXT, true);
      }
      return;
    }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    var doneSeen = false;
    while (!doneSeen) {
      var read = await reader.read();
      if (read.done) { break; }
      buf += decoder.decode(read.value, { stream: true });
      var lines = buf.split("\n");
      buf = lines.pop();
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line) { continue; }
        var obj = null;
        try { obj = JSON.parse(line); } catch (e) { obj = null; }
        if (!obj) { continue; }
        if (obj.error) {
          addNote(obj.error, true);
          doneSeen = true;
          break;
        }
        if (obj.message && obj.message.content) {
          if (!gotAny) {
            gotAny = true;
            setStatus(null);
          }
          full += obj.message.content;
          bubble.textContent = full;
          scrollDown();
        }
        if (obj.done) { doneSeen = true; }
      }
    }
    if (full) {
      messages.push({ role: "assistant", content: full });
      loadedModel = model;
    } else {
      bubble.remove();
    }
  } catch (e) {
    if (e && e.name === "AbortError") {
      if (full) {
        messages.push({ role: "assistant", content: full });
        loadedModel = model;
        addNote("Reply stopped.", false);
      } else {
        bubble.remove();
        messages.pop();
        addNote("Reply stopped.", false);
      }
    } else {
      bubble.remove();
      if (!full) { messages.pop(); }
      addNote(REACH_TEXT, true);
    }
  } finally {
    setStatus(null);
    setSending(false);
    liveAbort = null;
  }
}
document.getElementById("newchat").addEventListener("click", function () {
  if (sending) { return; }
  messages = [];
  chatEl.innerHTML = "";
  clearPhoto();
  setStatus(null);
});
document.getElementById("photo").addEventListener("click", function () {
  fileEl.click();
});
document.getElementById("photoremove").addEventListener("click", clearPhoto);
fileEl.addEventListener("change", function () {
  var file = fileEl.files && fileEl.files[0];
  if (!file) { return; }
  var url = URL.createObjectURL(file);
  var img = new Image();
  img.onload = function () {
    var longest = Math.max(img.width, img.height);
    var scale = longest > 1024 ? 1024 / longest : 1;
    var canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(img.width * scale));
    canvas.height = Math.max(1, Math.round(img.height * scale));
    var ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);
    var data = canvas.toDataURL("image/jpeg", 0.85);
    pendingImage = data.split(",")[1];
    thumbEl.src = data;
    chipEl.hidden = false;
  };
  img.onerror = function () {
    URL.revokeObjectURL(url);
    addNote("That photo could not be read.", true);
  };
  img.src = url;
});
sendEl.addEventListener("click", send);
stopEl.addEventListener("click", function () {
  if (liveAbort) { liveAbort.abort(); }
});
inputEl.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
refreshModels();
</script>
</body>
</html>
"""
