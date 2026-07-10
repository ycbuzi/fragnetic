## Fragnetic 20.15 — smarter coach grounding (semantic RAG via Ollama embeddings)

Completes the Ollama trio: your `nomic-embed-text` now powers the coach's knowledge
retrieval. No FPS impact.

### New
- **Semantic RAG.** When an Ollama embedding model is available (e.g. `nomic-embed-text`),
  the coach retrieves grounding facts by *meaning* instead of keyword overlap. Ask "how do I
  bring a teammate back to life" and it finds the "Life Saver card resurrects a downed ally"
  fact even though they share no words — the old keyword search would've missed it.
- Auto-detected (any Ollama model with `embed` in its name); falls back to the existing
  keyword retrieval when no embedding model is present. Fact vectors are embedded once and
  cached, so it's fast.
- The AI Coach badge shows `⚡ semantic RAG (model)` when it's active.

### The full Ollama picture now
- **Talk** → your text model (`qwen2.5:14b` etc.)
- **Vision** → an image-capable model if you pull one (`ollama pull qwen2.5vl:7b`)
- **Grounding** → `nomic-embed-text` for semantic fact retrieval

All optional, all auto-fallback to the bundled pieces, all on your machine.

Verified end-to-end against real `nomic-embed-text` (768-dim): a word-disjoint query ranked
the correct fact first. Update by downloading Fragnetic-Setup.zip below.
