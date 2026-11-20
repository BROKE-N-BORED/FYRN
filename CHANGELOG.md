# Changelog

## Fyrn v0.9 (2026-11-19)

The bridge for other programs, a reply-length setting,
honest fit badges, and idle auto-unload.

- **The bridge:** a new Settings switch, "Let other programs
  use Fyrn (bridge)", off by default. When it is on, Fyrn
  serves Ollama-shaped calls on this PC only (127.0.0.1,
  port 11434) while Fyrn is open, so a program that talks to
  Ollama, such as Code Forge, can run its models through
  Fyrn instead: one copy of the model in RAM, loaded and
  unloaded by Fyrn. Fyrn still holds one model at a time: a
  request naming another model loads it, and the same RAM
  fit check applies. Your own chat always wins: a request
  that arrives mid-reply is refused with a plain "busy"
  error, never by taking the model away, and Fyrn will not
  load, unload, or send while a bridge reply is running. If
  something else already holds the port, the status line
  says so and Fyrn runs on without the bridge. The full
  contract for client builders is in BRIDGE.md.
- **Reply length:** replies used to stop at 1024 tokens,
  which cut long answers off. Settings now offers the cap:
  512, 1024 (the standard), 2048, or 4096 tokens. It applies
  to plain replies and to tool-using replies alike.
- **Honest fit badges:** the fit check used to allow a flat
  64 MB for the engine's working memory. Real loads measured
  here show the engine's own buffers beyond the model file
  and its KV cache run to about 713 MB on the largest
  measured model (a 515 MB compute buffer alone on
  qwen3.5:2b), so the allowance is now 768 MB, and the
  no-header fallback rose from 512 MB to 1 GB. Badges and
  the load check use the new figures: at 4 GB free, the
  biggest models (minicpm5-heretic:2b, phi4-mini,
  qwen3.5:4b) now honestly read Tight where they read Fits
  before; at 6 GB free nothing in the roster changes.
- **Idle auto-unload:** a Settings choice (Off, 15, 30, or
  60 minutes; off by default). A loaded model that sits
  unused for that long unloads itself to give its RAM back,
  and the status line says it did. Sending a message,
  loading a model, and bridge requests all count as use and
  restart the wait; a reply or a bridge generation in
  progress is never unloaded from under.
- **Settings window:** the settings column now scrolls when
  it is taller than the screen, instead of squeezing its
  labels; Save and Cancel stay pinned at the bottom.
- The update check, pull, installer, saved chats, and tools
  are unchanged, and install.sh is byte-identical to v0.8.

## Fyrn v0.8 (2026-11-19)

Saved chats, instructions per chat, and tools the model can
call mid-chat.

- **Saved chats:** a chat saves itself as you talk, as a plain
  JSON file in ~/.fyrn/chats (one file per chat). The new
  sidebar on the Chat tab lists your chats, named from their
  first message; click one to reopen it with its whole
  conversation back in place. New chat is in the sidebar, the
  File menu (Ctrl+N), and the command palette; Rename and
  Delete work on the open chat, and Delete asks first with No
  as the answer it lands on. A chat reopens with no model
  loaded, or a different one: send, and the loaded model
  answers with the saved conversation as its context.
  Switching chats while a reply streams stops the reply first,
  the same as pressing Stop — a switch never silently loses a
  reply. Clear now empties the saved chat as well as the
  screen.
- **Instructions per chat:** the box above the transcript tells
  the model how to act in that chat ("You are…", "Always…").
  It is saved inside the chat file and sent as the chat's
  system prompt; a change applies from your next message;
  empty means no instructions.
- **Tools:** a model whose own chat template carries tool
  support can call tools mid-chat; Fyrn reads the support from
  the loaded model's template, so a model that cannot use
  tools simply chats as before, with one plain line on the
  Chat tab saying so. Every call shows in the transcript as it
  happens: the tool's name, what it did, and a short result
  line. Up to five tool rounds per reply; Stop cancels the
  whole loop. The tools:
  - **Files** — read and write text files and list folders,
    but only inside the tools folder (Settings, or the Tools
    folder button on the Chat tab; default ~/Fyrn files).
    A path that escapes the folder — .., a symlink, an
    absolute path outside — is refused, and the refusal goes
    back to the model and shows in the transcript. Reads are
    capped at 64 KB and say when they are cut.
  - **Web** — search and page reads go through Forge Browser,
    the browser app already on this PC, through its command
    line (search --json, fetch --json). Fyrn finds it in its
    usual install place (~/.local/share/forge-browser-app) or
    on PATH; Settings can point at a different folder. Without
    Forge Browser, the web tools decline plainly in the
    transcript and the rest still work.
  - **Browser view** — a new window showing what the web
    tools are doing: the current search or page, the text that
    came back, a screenshot when Forge Browser returns one,
    and an Open in Forge Browser button that opens the real
    Forge Browser on the same page. It opens on its own when a
    web tool runs, and from the Chat tab or the palette.
  - **Run a command** — the model can propose a command; Fyrn
    shows a modal with the exact command and the folder it
    runs in (the tools folder), and it runs only when you
    press Run — Cancel is the button it lands on. Output
    (capped) goes back to the model and shows in the
    transcript; 60-second limit. You never open a terminal:
    the model runs commands, you approve them.
  - **Time and calculator** — the current local date and
    time, and a safe arithmetic evaluator (an AST walk, no
    eval).
- **Settings** gains the tools folder picker and the optional
  Forge Browser folder. The command palette gains New chat,
  Rename chat, Delete chat, and Browser view.
- **Verified with a real model:** on the build machine, the
  real LFM3 1.7B (the roster's tool seat) loaded under the
  engine, reported tool support from its own template, and
  made two real tool calls end to end: it called the
  calculator for 17 * 24 and answered 408 from the result,
  and it read a notes file with read_file and answered with
  the launch code written in it. The full suite is 613 tests,
  all passing (3 skipped: the opt-in real-engine tests).

## Fyrn v0.7 (2026-11-18)

The engine bump: LFM3 loads now.

- **LFM3 loads:** the engine is now llama-cpp-python 0.3.37,
  whose bundled llama.cpp knows the LFM3 design (and qwen3vl).
  LFM3 1.7B loads and chats like any other model. The old
  "uses the LFM3 design, which Fyrn's engine does not support
  yet" message is gone for LFM3; the mechanism stays for any
  design the engine genuinely does not know yet.
- **The engine wheel ships inside the zip:** 0.3.37 has no
  pre-built wheel on PyPI or on the project's CPU wheel index
  (source only), so the release zip carries the wheel built
  from the 0.3.37 source (28 MB, in wheels/). The installer
  installs it first, so nothing ever compiles on your PC. The
  builder checks the wheel's SHA-256 on every build.
- **This release needs a reinstall:** the in-app update cannot
  bring the new engine, because the updater never touches the
  Python environment where the engine lives. To get v0.7,
  download it from the releases page and double-click
  Install-Fyrn again (your models folder and settings are
  untouched). Releases that only change Fyrn's own code will
  still update in-app.
- **LFM3's fit numbers are fixed:** the old code charged LFM3
  196,608 bytes of KV cache per context token, counting all 24
  layers at 16 KV heads. The model's header actually carries a
  per-layer head count: 11 layers cache KV (4 heads each) and
  13 are convolution layers that cache nothing. The real number
  is 22,528 bytes per token, measured against a real LFM3 load
  (the engine's own per-layer dims, cross-checked by memory
  measurement). The fit badge and the load check for LFM3 use
  the real number now.
- **Unload explains itself during a reply:** Unload is greyed
  out while a reply streams (unloading mid-reply would leave
  a reply coming from a model that is gone), but nothing said
  so — on a real PC it read as a broken button. The hero
  tile's stats line now carries "Reply in progress…" while a
  reply streams, so the greyed button is explained, and
  Unload says "Wait for the reply to finish first." instead
  of silently doing nothing. The guard itself is unchanged:
  unloading mid-reply is still refused.
- **Roster re-verified:** every roster model loads and
  generates under the new engine — all 11, plus qwen3:0.6b:
  both Qwen3.5 builds and the uncensored one, Qwen2.5-Coder,
  MiniCPM5-Heretic, Phi-4 mini, Granite, both Bonsai-2 builds,
  qwen3-vl, and LFM3 itself.
- **Vision works on the new engine:** qwen3-vl and
  Bonsai-2-uncensored both read a test picture correctly
  through their vision projectors (checked at engine level;
  Fyrn's own attach-a-photo flow is unchanged).

## Fyrn v0.6 (2026-11-18)

The hands-on audit fixes (17 of 18 tasks), plus the silent
update check. Every item below has a test.

- **Self-update:** Fyrn quietly checks GitHub for a newer
  release when it starts, in a background thread that never
  blocks startup. Up to date, or unreachable, means nothing
  happens at all. A newer release pops up and asks; nothing
  downloads until you tap to update. The download fetches the
  release zip and its checksum sidecar (new from this release:
  every release ships a `.sha256` file beside the zip) and
  verifies the SHA-256 before touching anything — a failed
  check means nothing is installed and the old version keeps
  running. Once verified, you choose: restart now and the
  update installs and Fyrn restarts on it, or wait and it
  applies at the next launch (re-verified first). The update
  replaces program files only — never the models folder,
  settings, or the venv — and files it replaces are backed up
  first, so a failed install restores the old version. Help
  menu, Check for updates, checks by hand; Settings turns the
  startup check off (it is on by default). Two things the
  update does not do: it does not touch the launcher or the
  desktop files (they point at the install folder, which does
  not move), and it never removes files a release dropped (it
  replaces and adds only).
- **Settings opens at full size:** the dialog used to open
  crushed to 560x220 with clipped labels and 9 px slivers for
  buttons. It now sizes from its content (bounded by the
  screen), so every label and button is whole.
- **First load is fast:** the first load of a session paid a
  70-90 s engine import inside the load worker, behind a
  misleading "Loading into RAM" message. The engine now warms
  up in the background at startup, so the first load starts
  loading at once; if the warm-up is still running, the load
  says "Starting the model engine..." honestly while it waits.
- **The grid stays in its cells:** at 56 models three cards
  piled on one spot. The hover lift captured a collapsed
  geometry mid-relayout and parked the card there forever. The
  lift now re-syncs to its true cell when it lands, the drop
  re-derives its target the same way, and a relayout cancels
  any lift in flight first.
- **The Pull window grows:** when the queue or unfinished list
  appears, the window grows to fit it (bounded by the screen)
  instead of crushing the lists into the empty window's space.
- **Ctrl+L respects the search:** loading a model the search
  is hiding used to load it unseen. It now refuses in plain
  words, the way Remove already did.
- **The Models menu follows state:** Load, Remove, and Unload
  enable and disable with the selection and the load state,
  the way the buttons already do. Load and Remove stay enabled
  for a hidden or Ollama selection, so their shortcuts reach
  the plain-words guard instead of doing nothing.
- **Keyboard focus shows the tooltip:** focusing a card with
  the keyboard used to show nothing (the tooltip call fired
  while the focus event was still being delivered, and Qt
  dropped it). It is deferred 100 ms now, and the tooltip
  appears.
- **A load failure names the design:** loading LFM3 used to
  say "The file may be broken", blaming a fine file for an
  engine that does not know the LFM3 design yet. It now says
  so plainly: "uses the LFM3 design, which Fyrn's engine does
  not support yet. The file itself is fine." (The engine bump
  that would let LFM3 load is still a separate decision, not
  in this release.)
- **The Pull window's Close button says what it does:** "Close
  this window (a running download stops, keeping its part
  file)".
- **Settings saves say what changed:** a settings-only save
  used to claim the models folder had moved. It now says
  "Settings saved.", and a save that changed the folder says
  so once, without the settings message clobbering it.
- **Names, never blob hashes:** loading an Ollama model used
  to show its blob filename in the status and in errors. The
  model's name is used everywhere now.
- **How to Use names all seven settings** (it said three).
- **Save chat:** the Chat tab and the File menu (Ctrl+S) save
  the transcript to a file you pick. This is the minimum
  scope: the file is the record, and there is no chat history
  across sessions yet.
- **The folder hint reads as a sentence:** "Models live in the
  folder set in the File menu, under Settings."
- **Tab skips the scroll area:** Tab used to land on the bare
  grid container with no visible focus. Keyboard scrolling
  still works (the grid scrolls to the focused card).
- **The double-click hint hides with the empty state:** with
  no models there is nothing to double-click.
- **One card is a third of the row:** a single model used to
  span the full width like the hero tile, and two took half
  each. Every column now takes an equal share, so a short row
  keeps the one-third card width and the hero tile's full
  width stays meaningful.

Not in this release: task 18 (badge hysteresis — the fit
badges still flip Tight/Too big as free RAM wobbles; the
current behaviour is defensible and no decision was made to
change it). The LFM3 engine gap (F1) is still open: LFM3
still will not load, it just fails plainly now.

## Fyrn v0.5 (2026-11-16)

The v0.2 review, finished: usability and polish. Every item
below has a test.

- Keyboard focus and selection are different things now, and they
  look different: a selected card has a solid amber border (the
  one Load will act on), a card that only has keyboard focus has
  a dashed blue ring. Both at once reads as selected.
- The app follows the system text size: no fixed point sizes
  anywhere in the app, so a larger system font scales every label
  and every card grows with it instead of clipping.
- Keyboard shortcuts: Ctrl+L load, Ctrl+P pull, Ctrl+I import,
  Ctrl+U unload, Delete remove, F5 refresh, Ctrl+F focus search,
  Ctrl+, settings, Ctrl+K command palette, Ctrl+Q quit, F1 How
  to Use. Every shortcut shows in its menu, and the buttons list
  theirs in their tooltips.
- Every button has a tooltip saying what it does (and its
  shortcut, when it has one). No button is left bare.
- All colours live in one place now (fyrn/theme.py): the dark
  theme's tokens, and a high-contrast theme beside them. The look
  is unchanged; only the source of the values moved.
- The pull progress view is tidier: the busy dot sits on the
  progress bar's row instead of its own row, and percent, MB,
  speed, and time left live in one stats line under the bar. The
  bars themselves carry no text (the numbers are in the stats
  line, where they stay readable).
- A second launch is fast: it raises the running window without
  importing Qt at all (a lock file plus a socket, stdlib only),
  instead of paying the full startup cost to discover Fyrn is
  already open. Measured with a real running instance: 0.37-0.60
  s per second launch, against the review's 11.49 s baseline.
- Cold start is faster: the pull module, the debug logger, and
  QtNetwork are imported only when they are actually needed
  instead of at startup. Measured by CPU time (the sandbox's
  wall clock is too load-noisy to claim seconds from): importing
  the window module costs 0.22 s of CPU against 0.30 s before,
  and the whole start to a shown window 0.33 s against 0.40 s.
- Command palette (Ctrl+K, or Help menu): every command in one
  searchable list. Type a few letters, press Enter.
- A pull finishing plays a short two-note chime (generated in
  code, no sound file, no new dependency), unless it is muted in
  Settings. It needs a sound backend: on a machine with no audio
  output device there is no chime, and the visual signal (the
  status fade) is the whole signal. The sound itself is not yet
  verified on a machine with audio.
- The model list moves: new cards fade in one after another,
  cards that change place in a re-sort glide to their new spot,
  and scrolling past the top or bottom shows a subtle edge glow.
  All of it is one-shot opacity and geometry work, cheap on a
  2-core CPU with no GPU, and all of it is skipped under Reduce
  motion.
- Compact model list (Settings): smaller cards, four across, so
  at least half again as many models fit on screen at once. The
  description and facts stay in each card's tooltip.
- High-contrast mode (Settings): black and white with yellow
  highlights, every bit of text at least 7 to 1 against its
  background. Applies live, no restart.

## Fyrn v0.4 (2026-11-16)

The v0.2 review, continued: install and first-run, then workflow.
Every item below has a test.

- Vision models can see. A model with its projector (mmproj)
  file next to it loads through the engine's vision handler (CPU
  only), and the Chat tab gains an Attach image button: pick a
  PNG or JPEG picture, ask about it. A vision model without its
  projector loads as text only and says so in the status line,
  and the fit badge counts the projector's size. Not yet verified
  with a real vision model: no Qwen3-VL-2B or similar model
  exists in the test sandbox, so the wiring is proven with the
  engine's handler mocked, and the real-model check is still
  owed on a PC that has one.
- Remove moves a model to the desktop trash instead of deleting
  it, so a mistaken remove is undoable from the trash. The
  confirm says so in plain words.
- Search and filter on the Models tab: a search box that narrows
  the list as you type, and a menu for size (under 1 GB, 1-3 GB,
  over 3 GB) or where the model came from (Ollama, my folder). A
  filter that matches nothing says so instead of leaving a blank
  tab. Load and Remove only work on a card you can see.
- Cards show real facts from the model's own header: parameter
  count, quantization, and context length ("2B · Q4_K_M · 32K
  context"), and the architecture in the hover detail. A file
  whose header carries nothing shows nothing: nothing is ever
  invented.
- Pulls can be queued: add several models, press Pull queue, and
  one worker pulls them in order, each with its own state
  (Waiting, Downloading, Done, Failed). A failure or a cancel
  stops the queue and leaves the rest waiting.
- Pulls show time left, smoothed so the number does not jump
  with every chunk ("about 3 min left"), and the queue position
  while a queue runs ("2 of 3").
- The pull field is checked the moment it loses focus: a bad
  model name or URL is an inline error right away, not a
  surprise when Pull is pressed.
- A model that is already in the folder can be pulled again: it
  asks first (default No, the same confirm habit as Remove),
  and the old file is kept until the new download finishes. A
  model that is loaded right now is refused outright, with a
  plain message to unload it first.
- Refreshing the model list no longer rebuilds the whole grid:
  cards that still show their model stay exactly where they are,
  so scroll position and keyboard focus survive every refresh.
  Only a model that changed (a re-pull, a load) gets a new card.
- Drop .gguf files on the window to import them, one after
  another, through the same worker as Import (progress, cancel,
  no partial model left behind).
- The installer checks free disk space first (1 GB floor) and
  refuses plainly when there is not enough, instead of dying
  mid-install with a broken half-install. When free space cannot
  be measured, it warns and carries on.
- The folder can move. Fyrn starts through a launcher that finds
  the folder again if it was moved or renamed, rewrites itself
  and the desktop icon to the new home, and starts Fyrn from
  there. When it cannot find the folder, or finds more than one,
  it says so plainly instead of guessing.
- Dependencies are pinned (PySide6-Essentials 6.12.0,
  llama-cpp-python 0.3.36), so an install today is the install
  that was tested.
- The zip test now covers the whole tree: a file forgotten from
  the builder's hand-maintained list fails the suite instead of
  shipping a zip without it.
- Local CI (BNB-TOOLS T28): `sh ci/check.sh` runs the suite,
  builds a throwaway release-shaped zip and verifies its stored
  modes, checks the executables carry their exec bits on disk,
  and writes the verdict to CI-STATUS.md.

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
