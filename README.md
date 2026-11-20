# Fyrn

**Run GGUF models on this PC, no other program needed.** Fyrn
is a standalone model runner for Linux: it loads a GGUF model file
and chats with it, on the CPU, with nothing in the cloud and no
other program involved. It is its own thing — it does not use the
Ollama program and does not speak the Ollama API.

It was built for a small PC (Ryzen 3, 8 GB RAM, no GPU), so it
checks before it loads: every model shows whether it fits this PC,
and a model that clearly will not fit is refused with a plain
message instead of crashing the machine.

The version lives in one place,
`fyrn/__init__.py` — nothing else in the code hardcodes it.

## Install (no terminal needed)

1. Unzip the release somewhere sensible, e.g. your home folder.
2. Double-click **Install-Fyrn**. It makes a private Python
   environment, installs what Fyrn needs, and adds a menu icon.
3. Start Fyrn from the menu icon.

If the double-click does not work, open a terminal in the folder
and run `bash install.sh` (bash, not sh).

If `python3-venv` is missing on your machine, the installer tells you
the one command that fixes it (`sudo apt install python3-venv`) and
stops — it never runs apt itself.

Updating from v0.6: v0.7 needs a reinstall, because the in-app
update cannot bring the new model engine (it never touches the
Python environment where the engine lives). Download v0.7 from
the releases page and double-click Install-Fyrn again — your
models folder and settings are untouched. Later releases that
only change Fyrn's own code will still update in-app.

## Use

- **Models tab:** every `.gguf` file in your models folder, with its
  size and a fit badge (Fits, Tight, Too big). A model with a
  description in its file shows it on its tile, and a model whose
  header carries facts shows them too ("2B · Q4_K_M · 32K
  context"). Search narrows the list as you type, and the menu
  narrows it by size or where the model came from. Fyrn also lists
  the models Ollama already downloaded on this PC, so you do not
  have to pull them again; those stay where Ollama put them, and
  Fyrn can load them but not remove them. Click a model to
  pick it, press Load (or double-click a model to load it straight
  away). Only one model runs at a time; loading a second one
  unloads the first. Press Unload when you are done to give the
  RAM back. Remove moves a model to the trash (it asks first),
  so you can get it back.
- **Pull models** (Models menu): type a Hugging Face model and its
  file name, like `myname/mymodel mymodel.gguf`, or paste a direct
  link to a `.gguf` file. Not sure what to get first? The Pull
  window suggests a small first model. Pulls show progress, MB
  downloaded, speed, and time left, and they resume after a
  failure or a cancel instead of starting over. Add several
  models to the queue and press Pull queue to pull them in
  order. A model that is already in the folder can be pulled
  again (it asks first). Unfinished downloads are listed
  at the bottom of the Pull window: Resume picks one up, Delete
  clears it away. A finished pull is checked against the checksum
  Hugging Face publishes.
- **Import a GGUF file** (Models menu): bring in a `.gguf` file from
  anywhere on the disk, such as a fine-tuned model you made
  elsewhere. It copies the file into your models folder, with
  progress and a cancel while it runs. Or drop `.gguf` files on
  the window: they import the same way, one after another.
- **Settings** (File menu): the models folder — pick any folder you
  like — whether Ollama's already-downloaded models are listed,
  a Reduce motion switch, Compact model list, High-contrast mode,
  the pull-complete chime mute, whether Fyrn checks for a
  newer release when it starts, the tools folder, an optional
  Forge Browser folder, the reply length (how long one answer
  may be, in tokens), the bridge for other programs, and idle
  auto-unload (free the RAM on its own after 15, 30, or 60
  unused minutes). Everything else (threads,
  context size) is set by the program itself to fit this PC.
- **Chat tab:** load a model, type a message (Enter sends, Shift+Enter
  starts a new line), press Send. The reply streams in, and the
  model remembers the conversation: each message carries the
  earlier turns with it. Live numbers
  show RAM used and free, speed in tokens per second, time to first
  token, and load time. A vision model (one with its projector
  file next to it) gains an Attach image button: pick a PNG or
  JPEG picture and ask about it.
- **Saved chats:** chats save themselves as you talk, as plain
  JSON files in `~/.fyrn/chats`. The sidebar on the Chat tab lists
  them, named from their first message; click one to reopen it
  with its whole conversation back in place, and keep chatting
  with whichever model is loaded. New chat (Ctrl+N) starts fresh;
  Rename and Delete work on the open chat. Clear empties the open
  chat; Save chat still writes a copy to a file you pick.
- **Instructions per chat:** the box above the transcript tells
  the model how to act in that chat ("You are…", "Always…"). It
  is saved with the chat and sent as its system prompt; a change
  applies from your next message; empty means none.
- **Tools:** a model whose own chat template supports tools can
  use them mid-chat, and every call shows in the transcript as it
  happens. It can read and write text files and list folders,
  but only inside the tools folder (Settings, or the Tools folder
  button on the Chat tab) — anything outside is refused, and the
  refusal shows. It can search and read the web through Forge
  Browser (the browser app on this PC): the Browser view window
  shows the search or page as it comes back, with a button that
  opens the real Forge Browser on the same page. It can ask to
  run a command: Fyrn shows the exact command first, and it runs
  only when you press Run. It can also ask for the current time
  and work out arithmetic. A model that cannot use tools just
  chats, exactly as before, and the Chat tab says so in one line.
- **Bridge for other programs:** an optional setting lets other
  programs on this PC (Code Forge, for one) run their models
  through Fyrn instead of Ollama: Fyrn serves Ollama-shaped
  calls on 127.0.0.1, port 11434, only while Fyrn is open.
  Off by default. Fyrn still holds one model at a time, the
  same RAM fit check applies, and your own chat always wins:
  a program asking mid-reply is told to wait. The full
  contract for client builders is in BRIDGE.md.
- **Updates:** Fyrn quietly checks GitHub for a newer release when
  it starts, and says so only when one is out. Nothing downloads
  until you tap to update; the download is checked against the
  release's checksum before anything is installed, and you choose
  whether to restart now or let it wait until the next restart.
  Help menu, Check for updates, checks by hand; Settings turns
  the startup check off.
- **Tray icon:** when your desktop has a system tray, Fyrn shows
  the loaded model and free RAM there; click it to bring Fyrn
  forward.
- **How to Use** (Help menu): every feature explained in plain words.
- **Debug report** (File menu): writes a report file with the log
  and the PC details, with any secrets removed. Send that file when
  asking for help.

## Notes

- Vision models work when their projector (mmproj) file sits in
  the same folder as the model: load the model and Attach image
  appears. A vision model without its projector loads as text
  only and says so.
- If you move or rename the Fyrn folder, the desktop icon still
  works: the launcher finds the folder again on its own. If it
  cannot find it, run `bash install.sh` again from the Fyrn
  folder.
- Models live in `~/Models` unless you pick a different folder in
  Settings.
- Logs live in `~/.fyrn/logs/`.
- The model engine is llama.cpp (CPU only), through
  `llama-cpp-python`. No network is needed except when pulling a
  model or checking for an update (the update check is one small
  request to GitHub's public releases page, and sends nothing
  about you or this PC).
- If motion on screen bothers you, turn on Reduce motion in
  Settings (File menu): the short fades and lifts are skipped
  and everything changes instantly instead.

## Development

Run the suite from the project folder:

```
python3 -m unittest discover -s tests
```

The suite uses a fake model engine with known replies and timing,
and a local fake download server, so it needs no real model and no
network. The pre-push check runs the suite, builds a throwaway
release-shaped zip, and verifies the file modes:

```
sh ci/check.sh
```

Build the release zip with:

```
python3 build_zip.py
```

The engine wheel (v0.7): llama-cpp-python 0.3.37 has no
pre-built wheel on PyPI or on the project's CPU wheel index
(source only), so the release zip carries a wheel built from
the 0.3.37 source tarball (built 2026-11-18 on Ubuntu 24.04,
CPU-only, runtime CPU dispatch). `build_zip.py` takes it from
the `FYRN_ENGINE_WHEEL` environment variable (default: the
verified build under `~/workspace/fyrn-engine-probe/wheels/`)
and checks its SHA-256 on every build; a missing or
mismatched wheel fails the build in plain words. The wheel is
a build-time input only — it never goes in the repo or the
push manifest. The installer installs it from the zip before
anything else, so no PC ever compiles the engine.

## License

MIT. See LICENSE.
