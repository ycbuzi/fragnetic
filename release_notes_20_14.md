## Fragnetic 20.14 — Ollama for voice AND vision

Extends the Ollama backend (20.13) so your local models power the coach's voice and its
eyes too. No FPS impact.

### Talk (voice)
- The hands-free voice coach already routes through your chosen Ollama model — same brain,
  spoken via the neural voice. Tip: pick an **instruct** model like `qwen2.5:14b` for snappy
  replies (thinking models like `qwen3` are smarter but slower per answer).

### Vision (the coach's eyes)
- **Optional Ollama vision.** If you pull an image-capable model, the coach uses it to read
  screenshots, maps, and scoreboards instead of the bundled vision model. Detected
  automatically via Ollama's model capabilities.
- **New Vision picker** in the AI Coach tab (next to the brain picker): **Bundled**,
  **Ollama · auto**, or a specific vision model. It shows a hint —
  `run: ollama pull qwen2.5vl:7b` — until you have one.
- **Opt-in + safe fallback.** Vision stays on the bundled model unless you pick an Ollama
  one, and any hiccup falls back automatically.

### Efficiency
- When Ollama is serving the coach, the app no longer spins up the bundled llama-server(s)
  in the background — leaving that GPU headroom for the game.

To use Ollama vision: `ollama pull qwen2.5vl:7b` (or `llava`, `llama3.2-vision`, `moondream`),
then pick it in the Vision dropdown. Update by downloading Fragnetic-Setup.zip below.
