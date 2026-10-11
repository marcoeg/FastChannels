# Open-source TTS for the voice agent — options on the local GPU

Written 2026-10-10. Today the agent speaks with ElevenLabs
`eleven_flash_v2_5` (voice "Bella"; `voice_agent.py`), with `openai.TTS()`
as the fallback when no ElevenLabs key is set. At 0.5 credit per
character that is roughly 1.4 ¢ a reply, ≈ $20 a month for the
household's traffic — about 98 % of the speech bill (STT,
`gpt-4o-mini-transcribe`, is ≈ $0.003 per audio minute, VAD-gated, a cent
a day). The laptop's RTX 5000 Ada 16 GB holds the 9B local brain (~9.5 GB
Q8 plus 1–2 GB of KV), leaving ~4 GB; nucbox has no GPU.

## The candidates, ranked for a voice assistant

| model | licence | size / VRAM | latency | quality | how it plugs in |
| --- | --- | --- | --- | --- | --- |
| **Kokoro-82M** | Apache-2.0 | 82 M, ~1 GB (runs on CPU too) | ~100–200 ms to first audio, 10–50× real time | clear, natural English; prosody flatter than ElevenLabs; no cloning | Kokoro-FastAPI serves an OpenAI-compatible `/v1/audio/speech`; the agent's `openai.TTS()` fallback takes a `base_url` — no code beyond a switch |
| **Orpheus-TTS 3B** | Apache-2.0 | 3 B Llama-based, ~3.5 GB as Q8 GGUF + the SNAC decoder | ~200 ms, streaming | closest to ElevenLabs among open models; emotive tags | runs on llama.cpp (orpheus-fastapi), OpenAI-compatible; tight next to the 9B — drop llama-server to 2 slots |
| **Kyutai TTS 1.6B** | CC-BY-4.0 | ~4–6 GB | ~220 ms, true text-streaming | very natural, built for agents | Rust `moshi-server`; needs a small LiveKit adapter |
| **Chatterbox 0.5B** | MIT | ~2–4 GB | a few hundred ms | expressive, zero-shot cloning from a consented sample | Python server; adapter needed |
| Piper | MIT | tiny, CPU | instant | robotic by comparison | last-resort fallback |

Skipped on licence or latency: XTTS-v2 (Coqui licence), F5-TTS and Fish
Speech (non-commercial), diffusion models (too slow for a reply).

## Recommendation

Kokoro first: zero marginal cost against the ElevenLabs bill, latency
better than Flash's network round trip, fits beside the 9B with room to
spare — or runs on nucbox's CPU and leaves the GPU alone. Integration is a
`VOICE_TTS=kokoro` switch pointing the OpenAI TTS plugin at the laptop's
Kokoro port with a voice name. Then a blind listen by the household
against Bella; if the prosody is not good enough, Orpheus on the same
llama.cpp is the step up, at the cost of VRAM.

## What to measure before switching

- Time to first audio chunk from the TTS request (the agent's `tts`
  metric; Flash today is a network round trip plus synthesis, ~300–500 ms).
- Naturalness in a blind A/B with the household on a dozen real replies
  (schedule answers with channel names and times are where flat prosody
  shows).
- Pronunciation of the lineup's names (KPYXDT, Gusto Español, Tr3s Latina)
  and of times ("six thirty", "11:08") — Kokoro has no vocabulary hint, so
  mispronunciations are fixed by spelling in the answer text, not in the
  model.
- VRAM on the laptop with both servers up (`nvidia-smi`), and the 9B's
  first-text latency unchanged.
