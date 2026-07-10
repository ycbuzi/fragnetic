## Fragnetic 20.13 — Ollama backend for the AI Coach

You can now point the coach at your own **Ollama** models instead of the bundled
llama-server. No FPS impact.

### New
- **Optional Ollama backend.** If Ollama is running (localhost:11434), the coach uses your
  local models automatically — no 2 GB model download, your choice of model (including
  bigger ones we'd never bundle), and Ollama manages the GPU.
- **Coach-brain picker** (AI Coach tab): choose **Bundled**, **Ollama · auto-pick**, or any
  specific installed model (e.g. `qwen2.5:14b`, `qwen2.5:32b`). Embedding-only models are
  filtered out. The badge shows which brain is answering.
- **Seamless fallback.** If Ollama isn't installed/running, or a request hiccups, the coach
  falls back to the bundled model — so nothing breaks for anyone without Ollama.
- Auto-pick skips non-chat models and prefers a real chat model.

### Notes
- Ollama's OpenAI-compatible API is used, so all existing coach features (RAG grounding,
  live-data injection, persona, voice) work unchanged through it.
- *Thinking* models (e.g. `qwen3`) give great answers but are slower per reply; for snappy
  in-game responses pick an instruct model like `qwen2.5:14b` in the dropdown.
- Verified end-to-end: backend detection, model listing, auto-pick, and a real grounded
  coach answer routed through Ollama.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
