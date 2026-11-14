# Changelog

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
