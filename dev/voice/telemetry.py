"""Latency telemetry for the voice pipeline — first metrics wiring here.

Two row families in one JSONL file (VOICE_METRICS_LOG, default
~/news/voice-metrics.jsonl):
- LiveKit metrics_collected events (eou/llm/tts/stt) with salient fields
- turn_timing rows from the adapter (ttfd, filler, tool spans)

Acceptance instrument for the latency target: on a no-tool turn,
eou.end_of_utterance_delay + llm.ttft + tts.ttfb < 1.0 s.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("voice.telemetry")

_LOG_PATH = Path(os.environ.get(
    "VOICE_METRICS_LOG",
    str(Path.home() / "news" / "voice-metrics.jsonl")))


def log_turn(row: dict) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **row}
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the pipeline
        logger.debug("metrics write failed", exc_info=True)


_FIELDS = {
    "eou_metrics": ("end_of_utterance_delay", "transcription_delay",
                    "on_user_turn_completed_delay"),
    "llm_metrics": ("ttft", "duration", "cancelled", "prompt_tokens",
                    "completion_tokens", "prompt_cached_tokens"),
    "tts_metrics": ("ttfb", "duration", "cancelled"),
    "stt_metrics": ("duration", "audio_duration"),
}


def wire_session_metrics(session) -> None:
    """Attach the metrics_collected handler to an AgentSession."""

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        try:
            m = getattr(ev, "metrics", ev)
            mtype = type(m).__name__
            key = {
                "EOUMetrics": "eou_metrics",
                "LLMMetrics": "llm_metrics",
                "TTSMetrics": "tts_metrics",
                "STTMetrics": "stt_metrics",
            }.get(mtype)
            if key is None:
                return
            row = {"type": key}
            for f in _FIELDS[key]:
                v = getattr(m, f, None)
                if v is not None:
                    row[f] = v
            log_turn(row)
        except Exception:  # noqa: BLE001
            logger.debug("metrics handler failed", exc_info=True)
