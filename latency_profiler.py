"""Latenz-Profiler: Detaillierte Aufschlüsselung der Antwort-Latenz pro Phase.

Misst folgende Phasen (End-to-End: User stoppt → Agent spricht):
1. User speech end → Agent state "thinking" (Endpointing + SDK-Overhead)
2. Agent "thinking" → Agent "speaking" (Gemini Inference + Audio-Pipeline)
3. Gemini TTFT + Duration (aus RealtimeModelMetrics, nachträglich)

Verwendung:
    profiler = LatencyProfiler()
    profiler.register(session)
"""

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TurnLatency:
    """Latenz-Daten für einen einzelnen User→Agent Turn."""
    turn_number: int = 0
    # Timestamps (perf_counter)
    user_speech_end: float = 0.0
    agent_thinking_start: float = 0.0
    agent_speaking_start: float = 0.0
    # Aus RealtimeModelMetrics (kommen nachträglich)
    gemini_ttft: float = 0.0
    gemini_duration: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_e2e(self) -> float:
        if self.user_speech_end and self.agent_speaking_start:
            return self.agent_speaking_start - self.user_speech_end
        return 0.0

    @property
    def best_latency(self) -> float:
        """Beste verfügbare Latenz-Metrik: Gemini TTFT > E2E."""
        if self.gemini_ttft > 0:
            return self.gemini_ttft
        return self.total_e2e

    @property
    def endpointing_plus_overhead(self) -> float:
        """Phase 1: User stoppt → Agent denkt (Endpointing + SDK-Scheduling)."""
        if self.user_speech_end and self.agent_thinking_start:
            return max(0, self.agent_thinking_start - self.user_speech_end)
        # Fallback: wenn thinking übersprungen wird, gesamte E2E als Phase 1
        if self.user_speech_end and self.agent_speaking_start and not self.agent_thinking_start:
            return self.total_e2e
        return 0.0

    @property
    def thinking_to_speaking(self) -> float:
        """Phase 2: Agent denkt → Agent spricht (Inference + Audio-Pipeline)."""
        if self.agent_thinking_start and self.agent_speaking_start:
            return max(0, self.agent_speaking_start - self.agent_thinking_start)
        return 0.0


@dataclass
class LatencyProfiler:
    """Sammelt detaillierte Latenz-Daten über alle Turns."""
    turns: list[TurnLatency] = field(default_factory=list)
    _current_turn: TurnLatency | None = None
    _turn_count: int = 0
    on_latency_measured: "callable | None" = None
    on_ttft_received: "callable | None" = None

    def register(self, session):
        """Registriert alle nötigen Event-Listener auf der AgentSession."""

        def on_user_state(event):
            logger.info("📊 [DEBUG] user_state_changed: %s → %s", event.old_state, event.new_state)
            if event.new_state == "listening" and event.old_state == "speaking":
                self._turn_count += 1
                self._current_turn = TurnLatency(turn_number=self._turn_count)
                self._current_turn.user_speech_end = time.perf_counter()
                logger.info("📊 [Turn %d] User stoppt (t=%.3f)", self._turn_count, self._current_turn.user_speech_end)

        def on_agent_state(event):
            logger.info("📊 [DEBUG] agent_state_changed: %s → %s", event.old_state, event.new_state)
            if self._current_turn is None:
                return
            now = time.perf_counter()

            if event.new_state == "thinking" and event.old_state in ("listening", "idle"):
                self._current_turn.agent_thinking_start = now
                delta = (now - self._current_turn.user_speech_end) * 1000
                logger.info("📊 [Turn %d] Agent denkt (%.0fms nach User-Stopp, t=%.3f)",
                           self._current_turn.turn_number, delta, now)

            elif event.new_state == "speaking" and event.old_state in ("thinking", "listening"):
                self._current_turn.agent_speaking_start = now
                turn = self._current_turn
                e2e = turn.total_e2e
                phase1 = turn.endpointing_plus_overhead
                phase2 = turn.thinking_to_speaking
                skipped = "(thinking übersprungen)" if not turn.agent_thinking_start else ""
                logger.info(
                    "📊 ═══ TURN %d ═══ %s\n"
                    "  ├── User→Thinking (Endpoint+SDK):  %6.0f ms\n"
                    "  ├── Thinking→Speaking (Inf+Audio):  %5.0f ms\n"
                    "  └── GESAMT E2E:                    %6.0f ms",
                    turn.turn_number, skipped, phase1 * 1000, phase2 * 1000, e2e * 1000
                )
                self.turns.append(turn)
                if self.on_latency_measured and e2e > 0:
                    e2e_values = [t.total_e2e for t in self.turns if t.total_e2e > 0]
                    avg = sum(e2e_values) / len(e2e_values)
                    self.on_latency_measured(e2e, avg)
                self._current_turn = None

        def on_metrics(event):
            metrics = getattr(event, 'metrics', event)
            cls_name = type(metrics).__name__
            if cls_name == 'RealtimeModelMetrics':
                ttft = getattr(metrics, 'ttft', 0) or 0
                duration = getattr(metrics, 'duration', 0) or 0
                in_tok = getattr(metrics, 'input_tokens', 0) or 0
                out_tok = getattr(metrics, 'output_tokens', 0) or 0
                # Attach to last turn
                if self.turns:
                    t = self.turns[-1]
                    t.gemini_ttft = ttft
                    t.gemini_duration = duration
                    t.input_tokens = in_tok
                    t.output_tokens = out_tok
                    logger.info(
                        "📊 [Turn %d] Gemini Metrics: TTFT=%.0fms, Duration=%.0fms, "
                        "Tokens in=%d out=%d",
                        t.turn_number, ttft * 1000, duration * 1000, in_tok, out_tok
                    )
                    # TTFT an Monitor senden (als genauere Latenz-Metrik)
                    if self.on_ttft_received and ttft > 0:
                        ttft_values = [t.gemini_ttft for t in self.turns if t.gemini_ttft > 0]
                        avg_ttft = sum(ttft_values) / len(ttft_values) if ttft_values else ttft
                        self.on_ttft_received(ttft, avg_ttft)
                elif self._current_turn:
                    self._current_turn.gemini_ttft = ttft
                    self._current_turn.gemini_duration = duration
                    self._current_turn.input_tokens = in_tok
                    self._current_turn.output_tokens = out_tok
                    logger.info(
                        "📊 [Turn %d] Gemini Metrics (pre): TTFT=%.0fms, Duration=%.0fms",
                        self._current_turn.turn_number, ttft * 1000, duration * 1000
                    )
            elif cls_name == 'EOUMetrics':
                eou_delay = getattr(metrics, 'end_of_utterance_delay', 0) or 0
                trans_delay = getattr(metrics, 'transcription_delay', 0) or 0
                turn_delay = getattr(metrics, 'on_user_turn_completed_delay', 0) or 0
                logger.info(
                    "📊 EOU Metrics: eou=%.0fms, transcription=%.0fms, turn_complete=%.0fms",
                    eou_delay * 1000, trans_delay * 1000, turn_delay * 1000
                )
            else:
                logger.debug("📊 Metrics: %s", cls_name)

        session.on("user_state_changed", on_user_state)
        session.on("agent_state_changed", on_agent_state)
        session.on("metrics_collected", on_metrics)
        logger.info("📊 LatencyProfiler registriert auf Session %s", type(session).__name__)

    def print_summary(self):
        """Gibt eine Zusammenfassung aller Turns aus."""
        if not self.turns:
            logger.info("📊 Keine Latenz-Daten gesammelt.")
            return

        e2e_values = [t.total_e2e for t in self.turns if t.total_e2e > 0]
        ttft_values = [t.gemini_ttft for t in self.turns if t.gemini_ttft > 0]

        def _stats(values):
            if not values:
                return "N/A"
            avg = sum(values) / len(values)
            mn = min(values)
            mx = max(values)
            return f"Ø {avg*1000:.0f}ms | Min {mn*1000:.0f}ms | Max {mx*1000:.0f}ms"

        summary = (
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║            LATENZ-ANALYSE ZUSAMMENFASSUNG                   ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            f"║ Turns gemessen: {len(self.turns):>3}                                       ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            f"║ End-to-End:        {_stats(e2e_values):<40}║\n"
            f"║ Gemini TTFT:       {_stats(ttft_values):<40}║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
        )

        if e2e_values:
            avg_e2e = sum(e2e_values) / len(e2e_values)
            avg_ttft = sum(ttft_values) / len(ttft_values) if ttft_values else 0
            avg_rest = max(0, avg_e2e - avg_ttft)

            pct_ttft = (avg_ttft / avg_e2e * 100) if avg_e2e > 0 else 0
            pct_rest = (avg_rest / avg_e2e * 100) if avg_e2e > 0 else 0

            summary += (
                "║ DURCHSCHNITTLICHE AUFTEILUNG:                              ║\n"
                f"║   Gemini TTFT:    {avg_ttft*1000:>6.0f}ms  ({pct_ttft:>4.1f}%)                     ║\n"
                f"║   Rest (EP+Net):  {avg_rest*1000:>6.0f}ms  ({pct_rest:>4.1f}%)                     ║\n"
                f"║   ─────────────────────────────────                         ║\n"
                f"║   GESAMT:         {avg_e2e*1000:>6.0f}ms  (100.0%)                     ║\n"
            )

        summary += "╚══════════════════════════════════════════════════════════════╝"
        logger.info(summary)

    def get_summary_dict(self) -> dict:
        """Gibt die Zusammenfassung als Dictionary zurück."""
        if not self.turns:
            return {"turns": 0, "message": "Keine Daten"}

        e2e_values = [t.total_e2e for t in self.turns if t.total_e2e > 0]
        ttft_values = [t.gemini_ttft for t in self.turns if t.gemini_ttft > 0]

        def _avg(v): return round(sum(v)/len(v)*1000) if v else 0

        return {
            "turns": len(self.turns),
            "e2e_avg_ms": _avg(e2e_values),
            "e2e_min_ms": round(min(e2e_values)*1000) if e2e_values else 0,
            "e2e_max_ms": round(max(e2e_values)*1000) if e2e_values else 0,
            "gemini_ttft_avg_ms": _avg(ttft_values),
            "best_latency_avg_ms": _avg([t.best_latency for t in self.turns if t.best_latency > 0]),
            "per_turn": [
                {
                    "turn": t.turn_number,
                    "e2e_ms": round(t.total_e2e * 1000),
                    "best_latency_ms": round(t.best_latency * 1000),
                    "phase1_endpoint_sdk_ms": round(t.endpointing_plus_overhead * 1000),
                    "phase2_inference_audio_ms": round(t.thinking_to_speaking * 1000),
                    "gemini_ttft_ms": round(t.gemini_ttft * 1000),
                    "gemini_duration_ms": round(t.gemini_duration * 1000),
                    "tokens_in": t.input_tokens,
                    "tokens_out": t.output_tokens,
                }
                for t in self.turns
            ],
        }
