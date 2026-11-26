# The Fyrn bridge

Fyrn can lend its engine to other programs on the same PC.
When the bridge is on, Fyrn runs a small local web server
that speaks the same shapes as Ollama's API, so a program
that already talks to Ollama can talk to Fyrn instead, and
the model runs inside Fyrn: one copy in RAM, loaded and
unloaded by Fyrn, with Fyrn's fit check in front of it.

The bridge is an adapter on the side. Fyrn itself is not an
Ollama clone and does not try to be; the bridge only covers
the handful of calls a local client needs.

## Turning it on

In Fyrn: Settings, then check "Let other programs use Fyrn
(bridge)", then Save. The change applies at once, no restart.
The toggle is off by default, and when it is off nothing
listens on any port.

When it is on, Fyrn listens on 127.0.0.1, port 11434 (the
port Ollama uses, so existing clients find it where they
expect). Only this PC can reach it; nothing is exposed to
the network. If something else already holds the port (a
running Ollama, most likely), Fyrn says so in its status
line and keeps running without the bridge; close the other
program and toggle the setting off and on to retry.

Closing Fyrn closes the bridge.

## Sharing on the home network, and the phone deck

A second Settings checkbox, "Share Fyrn on my home
network", widens the bridge (v0.11). It is off by default
and only matters while the bridge is on:

- Bridge on, sharing off: as above, 127.0.0.1 only.
- Bridge on, sharing on: the same server binds 0.0.0.0
  instead, so anything on the home network can reach every
  endpoint in this document. There is no access code and
  no login: while sharing is on, anyone on the home WiFi
  can use the models. Saving either toggle applies it at
  once (a sharing change restarts the bridge on the new
  address); no app restart is needed.

Sharing exists for the phone deck: a small chat page the
bridge serves at GET /deck (see below), so the user can
chat with the PC's models from a phone on the same WiFi.
The Phone deck button on the Models tab turns the bridge
and sharing on together (both are saved) and shows the
deck's address, http://<the PC's network
address>:11434/deck, as a QR code; Settings shows the
same address while sharing is on. The deck window only
shows a code when the bridge is actually running shared:
if the bridge cannot start, it says why instead. When
the PC's network address cannot be found, Fyrn says so
instead of showing an address that would not work.

## The rules a client must live with

- **One model at a time.** Fyrn holds a single loaded model.
  A request naming a different model unloads the current one
  and loads the named one first, which takes as long as a
  load takes. A request naming the loaded model reuses it.
- **The GUI wins.** If the person using Fyrn is mid-reply or
  mid-tool-loop, generation requests are refused with HTTP
  409 and a plain-words error, never by yanking the model
  away. Retry when the reply finishes. The same applies in
  reverse: Fyrn will not load, unload, or send while a bridge
  generation is running.
- **One bridge generation at a time.** A second generate or
  chat request while one is running gets the same 409. A
  request with tools or images is still one generation.
- **The RAM fit check applies.** A model that does not fit
  the PC's free RAM is refused with HTTP 500 and the same
  wording the GUI would show. There is no force option.
- **Tools pass through; the bridge never runs them.** A chat
  request may carry `tools` (the client's own definitions).
  They are offered to the model as they are, and the model's
  tool calls come back in the reply's `message.tool_calls`.
  The bridge itself executes nothing: no file access, no
  commands, no approval prompts. The client runs the calls,
  appends the results as `role: "tool"` messages, and calls
  again — each HTTP request is exactly one model turn.
- **Images are carried, or refused out loud.** A message's
  `images` (base64) go to the model when the loaded model
  can see (a vision model with its projector). When it
  cannot, the request is refused with HTTP 400 and a plain
  "cannot see pictures" error — an image is never silently
  dropped.
- **Stopping works like Ollama.** If the client disconnects
  mid-stream, Fyrn stops generating.

## Endpoints

All bodies are JSON. Errors are `{"error": "..."}` with a
matching HTTP status.

### GET /api/tags

The models Fyrn can see: the models folder plus, when the
"List Ollama models too" setting is on, the ones Ollama has
downloaded.

```json
{"models": [
  {"name": "qwen3:0.6b",
   "model": "qwen3:0.6b",
   "size": 522640096,
   "modified_at": "2026-11-19T10:24:31-05:00",
   "digest": "sha256:7f4030..."}
]}
```

`digest` appears only for models stored as Ollama blobs,
whose file name is their digest. `name` is what a generate
or chat request should send as `model`. A name without a
`:tag` matches the `:latest` model of that name, as in
Ollama.

### GET /api/version

```json
{"version": "0.10"}
```

The value is Fyrn's own version, not Ollama's. A client
that needs to know it is talking to Fyrn can check that
this is a Fyrn version number.

### GET /api/ps

The model Fyrn currently holds, or an empty list:

```json
{"models": [
  {"name": "qwen3:0.6b",
   "model": "qwen3:0.6b",
   "size": 522640096,
   "digest": "sha256:7f4030...",
   "expires_at": "2026-11-19T15:42:00.000000Z"}
]}
```

`digest` appears only for Ollama-store models. `expires_at`
appears only when Fyrn's idle auto-unload setting is on: it
is the moment the model would unload itself if nothing else
uses it (any use restarts the wait, and a later read then
projects the new moment). /api/ps is a status read: asking
does not count as use and never keeps a model alive.

### GET /

Answers `Fyrn bridge is running.` as plain text. Handy for
a "is anything there?" probe.

### GET /deck

The phone deck: one self-contained HTML page (no external
assets; it works on a network with no internet), served
with Content-Type text/html whenever the bridge is on, on
whichever address the bridge bound. It is a chat face for
a phone: a model picker fed by /api/tags with the loaded
model (from /api/ps) marked, streaming chat over
/api/chat, a stop button, one photo per message (shrunk
on the phone to at most 1024 px and sent as the message's
`images`, exactly as the Images section describes), and a
new-chat button. Deck requests are bridge requests: the
busy rules above apply to them unchanged. The deck has no
tools, no settings, and no file access.

### POST /api/generate

Request:

```json
{"model": "qwen3:0.6b",
 "prompt": "Why is the sky blue?",
 "stream": true,
 "options": {"num_predict": 256},
 "system": "Answer like a pirate."}
```

- `model` (required): a name from /api/tags.
- `prompt` (required): the text to complete.
- `stream`: defaults to true, as in Ollama.
- `system`: optional instructions for this reply.
- `images`: optional list of base64 pictures (PNG or JPEG)
  to send with the prompt. The loaded model must be a
  vision model; a model that cannot see gets the request
  refused with HTTP 400 (see the rules above).
- `options.num_predict`: optional reply-length cap for
  this request. Without it, Fyrn's own reply-length
  setting applies.
- `options.num_ctx`: optional context size in tokens. If
  it differs from the size the model is loaded with, the
  model is reloaded at the requested size first (the RAM
  fit check prices the KV cache at that size). The value
  is clamped to at least 128 and, when the model's GGUF
  header names its trained context, to that.
- `keep_alive`: the value 0 (or "0"/"0s") unloads the
  model after this reply completes. An otherwise empty
  request (no prompt) with `keep_alive: 0` unloads the
  named model at once and answers `done` — Ollama's unload
  idiom. Any other value is accepted and ignored: Fyrn's
  idle auto-unload setting governs how long a model stays
  loaded.

Streaming response: one JSON object per line. Text lines:

```json
{"model": "qwen3:0.6b", "created_at": "...", "response": "The sky", "done": false}
```

and a final line with `"done": true`, an empty `response`,
and the timings: `total_duration`, `load_duration`,
`prompt_eval_count`, `prompt_eval_duration`, `eval_count`,
`eval_duration` (durations in nanoseconds, as in Ollama).

With `"stream": false`, a single JSON object with the whole
`response` and the same final fields.

### POST /api/chat

Request:

```json
{"model": "qwen3:0.6b",
 "messages": [
   {"role": "system", "content": "Be brief."},
   {"role": "user", "content": "Hello"},
   {"role": "assistant", "content": "Hi there."},
   {"role": "user", "content": "Tell me a joke."}],
 "stream": true,
 "options": {"num_predict": 256}}
```

How Fyrn reads the messages: `system` messages join into
the chat's instructions; the last `user` message is the
prompt; the `user` and `assistant` turns before it are the
history the model sees. At least one `user` message is
required (400 otherwise).

Streaming response lines carry the text as
`{"message": {"role": "assistant", "content": "..."}, "done": false}`,
with a final `"done": true` line whose message content is
empty and which carries the same timing fields as
generate. Non-streaming returns one object with the full
`message`.

#### Tools (pass-through)

Add `tools` — your own tool definitions in Ollama's shape
(`[{"type": "function", "function": {"name": ...,
"description": ..., "parameters": {...}}}]`) — and the
request becomes one tool turn:

- The definitions are offered to the model untouched. If
  the model asks for tools, the reply's message carries
  them: `"tool_calls": [{"function": {"name": "calculate",
  "arguments": {"expression": "17 * 24"}}}]` (arguments as
  an object), with the message content usually empty.
- **The bridge does not run the calls.** Your program runs
  them, then calls /api/chat again with the conversation
  extended: the assistant turn carrying its `tool_calls`,
  then one `{"role": "tool", "content": "<result>"}` message
  per result. Fyrn feeds both back to the model, and the
  next reply is the model's answer built on the results —
  or another round of calls.
- A model whose chat template has no tool support answers
  in plain text, as Ollama does with such a model: the
  reply simply has no `tool_calls`. The bridge never
  invents calls a model did not make.
- Tool turns do not stream piece by piece. With
  `"stream": true` a tool turn arrives as one line carrying
  the message (content and any `tool_calls`), then the
  closing `"done": true` line.
- `tools` on /api/generate is ignored: tool turns are a
  chat-endpoint shape, as in Ollama.

#### Images

Any message may carry `images`: a list of base64 pictures
(PNG or JPEG). They are sent with the conversation to the
model, which must be a vision model (its projector loaded);
a model that cannot see gets the whole request refused
with HTTP 400 and an error saying the model cannot see
pictures. Unreadable base64, or bytes that are neither PNG
nor JPEG, are also a 400. The pictures are decoded to a
temporary file inside Fyrn's home folder for the one
request and deleted when the reply finishes.

#### keep_alive and num_ctx on chat

Chat takes the same `keep_alive` and `options.num_ctx` as
generate, with the same meanings. The empty unload request
on this endpoint is a body with the model, an empty
`messages` list, and `keep_alive: 0`.

### POST /api/show

What Fyrn knows about one model. Request:
`{"model": "qwen3:0.6b"}` (`name` is accepted too). Fyrn
fills only what it can read honestly:

```json
{"name": "qwen3:0.6b",
 "model": "qwen3:0.6b",
 "size": 522640096,
 "digest": "sha256:7f4030...",
 "details": {"format": "gguf", "family": "qwen3",
             "families": ["qwen3"],
             "parameter_size": "0.6B",
             "quantization_level": "Q4_K_M"},
 "model_info": {"general.architecture": "qwen3",
                "qwen3.context_length": 262144},
 "loaded_context_tokens": 2048}
```

- `details.format` is always `"gguf"`; `details.family`
  (and `families`) is the architecture from the GGUF
  header, empty when the header cannot be read.
  `parameter_size` and `quantization_level` appear when
  the header carries them.
- `model_info` carries `general.architecture` and
  `"<arch>.context_length"`: the context the model was
  TRAINED for, from its header.
- `loaded_context_tokens` appears only when this model is
  the one currently loaded: the context size it is
  actually running with (Fyrn's machine pick, or a
  request's `num_ctx`). A client budgeting context should
  read the effective window from here when the model is
  loaded, and from `model_info`'s trained length as the
  ceiling when it is not.
- An unknown model is a 404; a missing model name is a
  400. /api/show is a status read: asking does not count
  as use.

## Status codes

- 200: fine.
- 400: the request is malformed (bad JSON, no model, no
  prompt, no user message, tools that are not a list of
  definitions), or it carries a picture the loaded model
  cannot see, or picture data that is not readable base64
  PNG/JPEG.
- 404: the named model is not one Fyrn can see, or the
  path is not a bridge endpoint.
- 409: busy. The GUI is mid-reply or mid-tool-loop, or
  another bridge generation is running. Retry shortly.
- 500: the model could not be loaded: it does not fit, the
  file is broken, or the engine refused it. The error text
  says which.

## A minimal client

Anything that can speak HTTP to 127.0.0.1:11434 works. With
curl:

```
curl http://127.0.0.1:11434/api/generate -d '{"model": "qwen3:0.6b", "prompt": "Hello", "stream": false}'
```

From Python, with nothing but the standard library:

```python
import json, urllib.request

body = json.dumps({"model": "qwen3:0.6b",
                   "prompt": "Hello",
                   "stream": False}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:11434/api/generate", data=body,
    headers={"Content-Type": "application/json"})
reply = json.loads(urllib.request.urlopen(req).read())
print(reply["response"])
```

Point an existing Ollama client's base URL at
`http://127.0.0.1:11434` and it will find the same shapes
for the endpoints above. Endpoints not listed here are not
implemented; calls to them return 404.
