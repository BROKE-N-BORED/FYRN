# Changelog

## Fyrn v0.3 (2026-11-15)

The v0.2 review, built: the bugs and correctness findings, highest
first. Every item below was found by reading the code and driving
the app, and every one has a test.

- Fyrn finds the models already on the PC. It lists the models
  Ollama already downloaded (from Ollama's own store) alongside
  the models folder, so a model that is already on the disk never
  needs pulling or typing in. Those models stay where Ollama put
  them: Fyrn can load them, but it cannot remove them. Turn the
  listing off in Settings if you only want your models folder.
- Closing the window during a load no longer crashes the process.
  A real load cannot be stopped once it starts, so Fyrn waits for
  it to finish and then closes, instead of destroying a running
  thread and aborting.
- A failed load resets the screen to "nothing loaded": the hero
  tile, the Loaded pill, and the stale stats go away, and the
  chat hint goes back to the true one. A refused load (too big
  for RAM) still leaves the current model loaded, as before.
- Load cancel is honest now: on the real backend the button is
  greyed out and the status says the load cannot be stopped,
  because the backend never checks for a cancel mid-load.
- Load, Unload, and Remove stay disabled while a reply streams,
  so a reply can no longer come from a model the screen says is
  gone (or deleted).
- Import runs on a worker thread with progress and a cancel: a
  4 GB import no longer freezes the window for ~40 seconds. The
  copy lands in a .part file and is moved into place only when
  complete, so a stopped or failed import leaves no partial
  model behind. Disk space is checked before the copy starts.
- Cards in a grid row share the same top edge: a card with a
  description no longer sits above its neighbours.
- The press-dip test no longer asserts a transient animation
  state after a fixed wait: it polls until the button settles.
  The suite failure that used to come and go is gone. Two more
  tests with the same fixed-wait race (card hover lift, status
  fallback) poll until settled the same way.
- A model file vanishing between the existence check and the
  size check is reported as a missing model, not an uncaught
  error.
- The model remembers the conversation: each message carries the
  earlier turns with it, trimmed to fit the context window
  (oldest first). Clear empties the history along with the
  transcript.
- Replies are capped at 1024 tokens, so a model that never
  stops talking stops on its own instead of running to context
  fill.
- The fit check uses the real KV cache size from the model's own
  GGUF header (layers, heads, head size, context length) instead
  of a fixed 512 MB guess, so it stops over-promising on the
  bigger models.
- Pulls are verified against the checksum Hugging Face publishes:
  a corrupt download is deleted, never saved as a model. A resume
  against an upstream file that changed starts over instead of
  mixing old and new bytes (the server's ETag is tracked with
  the partial file).
- Unfinished downloads are listed at the bottom of the Pull
  window, with Resume and Delete. A resume needs no retyping:
  the address travels with the partial file.
- Clear also clears the stats line: the numbers no longer
  describe a conversation that no longer exists.
- Load is not offered for the model that is already loaded.
- The dead VERSION constant in core.py is gone, and the README
  line about where the version lives is true again.

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
