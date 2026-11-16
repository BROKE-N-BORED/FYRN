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

## Use

- **Models tab:** every `.gguf` file in your models folder, with its
  size and a fit badge (Fits, Tight, Too big). A model with a
  description in its file shows it on its tile. Fyrn also lists
  the models Ollama already downloaded on this PC, so you do not
  have to pull them again; those stay where Ollama put them, and
  Fyrn can load them but not remove them. Click a model to
  pick it, press Load (or double-click a model to load it straight
  away). Only one model runs at a time; loading a second one
  unloads the first. Press Unload when you are done to give the
  RAM back. Remove deletes a model from the folder (it asks first).
- **Pull models** (Models menu): type a Hugging Face model and its
  file name, like `myname/mymodel mymodel.gguf`, or paste a direct
  link to a `.gguf` file. Not sure what to get first? The Pull
  window suggests a small first model. Pulls show progress, MB
  downloaded, and speed, and they resume after a failure or a
  cancel instead of starting over. Unfinished downloads are listed
  at the bottom of the Pull window: Resume picks one up, Delete
  clears it away. A finished pull is checked against the checksum
  Hugging Face publishes.
- **Import a GGUF file** (Models menu): bring in a `.gguf` file from
  anywhere on the disk, such as a fine-tuned model you made
  elsewhere. It copies the file into your models folder, with
  progress and a cancel while it runs.
- **Settings** (File menu): the models folder — pick any folder you
  like — a Reduce motion switch, and whether Ollama's already-
  downloaded models are listed. Everything else (threads,
  context size) is set by the program itself to fit this PC.
- **Chat tab:** load a model, type a message (Enter sends, Shift+Enter
  starts a new line), press Send. The reply streams in, and the
  model remembers the conversation: each message carries the
  earlier turns with it. Clear empties the transcript. Live numbers
  show RAM used and free, speed in tokens per second, time to first
  token, and load time.
- **Tray icon:** when your desktop has a system tray, Fyrn shows
  the loaded model and free RAM there; click it to bring Fyrn
  forward.
- **How to Use** (Help menu): every feature explained in plain words.
- **Debug report** (File menu): writes a report file with the log
  and the PC details, with any secrets removed. Send that file when
  asking for help.

## Notes

- Text chat only in this version. Vision models come later.
- Models live in `~/Models` unless you pick a different folder in
  Settings.
- Logs live in `~/.fyrn/logs/`.
- The model engine is llama.cpp (CPU only), through
  `llama-cpp-python`. No network is needed except when pulling a
  model.
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
network. Build the release zip with:

```
python3 build_zip.py
```

## License

MIT. See LICENSE.
