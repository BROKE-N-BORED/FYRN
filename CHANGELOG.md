# Changelog

## Fyrn v0.2 (2026-11-13)

The v0.1 UI audit, finished: the 10 low findings, highest first.

- How to Use opens with a "First steps" section: get a model, load
  it, chat. A start-here path for readers.
- Status-bar messages time out after 5 seconds and fall back to
  the neutral line, so stale news never lingers on an unrelated
  screen. Live states (loading, stopping) stay until they end.
- Pull errors follow one pattern: a bad input or an already-there
  model is a quiet inline error in the status line, no modal. Only
  a download failure keeps the modal, because it happens while you
  may be elsewhere.
- Skeleton tiles are grey shape blocks (name bar, size bar, badge
  bar) instead of three tiles reading "Loading models...".
- The chat input is multi-line: Enter sends, Shift+Enter starts a
  new line. It grows from one line to four as the message grows.
- A Clear button empties the chat transcript. Nothing is saved, so
  nothing is lost and no confirm is needed.
- Pull and Settings windows hug their content: the dead space the
  audit saw was already filled by the v0.1.1 additions (the first-
  model suggestion, the Reduce motion switch), verified and pinned
  by tests here.
- Model cards show the description from the file's own GGUF header
  (general.description), clipped to a glance, and in the hover
  detail. A file with no description shows none: descriptions are
  read from the file, never invented.
- Tray icon (when the desktop has one): the loaded model and free
  RAM at a glance, one click to bring Fyrn forward. No tray, no
  icon, no complaint.
- Not built: per-model profiles (L7). They earn their place only
  when a real per-model setting exists to remember, and there is
  still none: Reduce motion is global, and threads and context are
  set by the program itself. No setting was invented to justify
  them.

## Fyrn v0.1.1 (2026-11-13)

The v0.1 UI audit, built: the 12 medium findings, highest first.

- Pull window suggests a real first model (a small Qwen2.5 0.5B,
  about 400 MB) with one tap to fill the field, and says plainly
  that models come from Hugging Face, a website where models are
  listed.
- Load progress is honest on the real backend: an indeterminate bar
  with stage text instead of a percent that measured nothing.
- Remove a model from the Models tab, the Models menu, or a card's
  right-click menu; it asks first, names the file and its size,
  and defaults to No. A loaded model is unloaded first.
- Settings gains a Reduce motion switch, stored in settings; the
  environment variable stays as an override.
- The bento grid is a real bento now: 3 columns, 16 px gaps,
  shorter cards, and the hero tile for the loaded model shows its
  live stats.
- Hovering (or focusing) a model card shows its full path, size,
  RAM needed vs free, and why it fits or not.
- Chat replies carry the model name that wrote them, and turns are
  separated by a divider line.
- The selection survives list refreshes; importing or pulling a
  model selects the new model.
- Closing the Pull window mid-pull asks first ("Stop this pull?"),
  defaulting to No, and says the part already downloaded stays.
- Micro-interactions: buttons dip 3% when pressed (150 ms), cards
  lift 4 px on hover (200 ms). Skipped under reduced motion.
- Double-click to load is documented in the Models tab and How to
  Use.
- Pull, Settings, and How to Use remember their size and position
  between runs, like the main window.
- Fixed in passing: labels inside cards and panels no longer show
  a darker band behind the text.

## Fyrn v0.1.0 (2026-11-13)

First release. Built under the working name Own Ollama; renamed to
Fyrn for release.

- Standalone GGUF model runner for Linux, CPU only. No Ollama
  program needed, no Ollama API spoken.
- Models tab: models as tiles with size and a fit badge (Fits,
  Tight, Too big), skeleton tiles while the folder loads, hero tile
  for the loaded model. An empty folder shows a setup checklist
  (pull or import, load, chat) instead of a blank tab.
- Model cards work by keyboard: Tab moves between them, Space
  picks the one in focus, Enter loads it. Buttons show a visible
  amber focus ring.
- Load one model at a time with a RAM check first (refuses a model
  that will not fit, warns when tight); loading a second model
  unloads the first. Load runs in a worker thread with progress
  and cancel.
- Pull window (Models menu): type a Hugging Face model or paste a
  direct .gguf link; resumable downloads with progress, MB
  downloaded, speed, and cancel.
- Import a loose .gguf file into the models folder.
- Settings window (File menu): the models folder, and nothing else.
- Chat tab: streaming replies, Stop button, live RAM and speed
  numbers (tokens per second, time to first token, load time).
- How to Use index in the Help menu.
- Debug log layer with a Debug report button (File menu).
- Single instance: a second launch raises the running window.
- GUI double-click installer, menu icon, desktop icon, and
  `fyrn` launcher command (works from any folder). The installer
  pulls the model engine as a pre-built CPU wheel, so no
  compiler is needed on the machine.
