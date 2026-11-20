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
  chat request while one is running gets the same 409.
- **The RAM fit check applies.** A model that does not fit
  the PC's free RAM is refused with HTTP 500 and the same
  wording the GUI would show. There is no force option.
- **No tools.** Tool calls are a GUI feature. The bridge
  never offers tools to the model and ignores tool-related
  fields in requests.
- **No images yet.** Image fields in chat messages are not
  carried to the model. Text only.
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
{"version": "0.9"}
```

The value is Fyrn's own version, not Ollama's. A client
that needs to know it is talking to Fyrn can check that
this is a Fyrn version number.

### GET /

Answers `Fyrn bridge is running.` as plain text. Handy for
a "is anything there?" probe.

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
- `options.num_predict`: optional reply-length cap for
  this request. Without it, Fyrn's own reply-length
  setting applies.

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

## Status codes

- 200: fine.
- 400: the request is malformed (bad JSON, no model, no
  prompt, no user message).
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
