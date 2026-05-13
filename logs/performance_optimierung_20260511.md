# Performance-Optimierung — 11.05.2026

## Ziel
Deutlich geringere Turn-Latenz (Zeit von User-Sprachende bis Agent-Sprechbeginn), ohne Stabilitätsverlust.

---

## Messgrundlage

Der `LatencyProfiler` teilt Turn-Latenz in zwei Phasen auf:
- **Phase 1** (Endpointing+SDK): User stoppt → Agent geht in "thinking" → dominiert von `silenceDurationMs`
- **Phase 2** (Inferenz+Audio): Agent "thinking" → Agent spricht → dominiert von Gemini-Inferenzzeit

---

## Durchgeführte Änderungen (11.05.2026)

### 1. `proactivity` explizit als `ProactivityConfig` gesetzt (`main_livekit.py`)

**Vorher:** `proactivity=True`
**Nachher:** `proactivity=genai_types.ProactivityConfig(proactive_audio=True)`

**Warum:** Mit `True` als Python-Bool war unklar, ob das LiveKit-Plugin den Parameter korrekt
an die Gemini Live API weitergab oder stilll ignorierte. `ProactivityConfig(proactive_audio=True)`
ist die explizite Gemini-API-Konfiguration. Proactive Audio erlaubt Gemini, mit der
Antwortgenerierung zu beginnen, *bevor* der User fertig gesprochen hat (spekulatives Dekodieren).

**Geschätzter Gewinn:** −300 bis −1000ms Phase 1+2 (wenn vorher inaktiv)

---

### 2. `thinking_config` von `None` auf `ThinkingConfig(thinking_budget=0)` (`main_livekit.py`)

**Vorher:** `thinking_config=None`
**Nachher:** `thinking_config=genai_types.ThinkingConfig(thinking_budget=0)`

**Warum:** `None` bedeutet Gemini-Default. Bei Gemini 2.5 Flash könnte der Default Thinking
aktivieren (variable Anzahl Thinking-Tokens je nach Anfrage-Komplexität). `thinking_budget=0`
erzwingt exakt 0 Thinking-Tokens, unabhängig vom Modell-Default.

**Geschätzter Gewinn:** −0 bis −500ms Phase 2 (wenn Thinking vorher aktiv war)

---

### 3. `silenceDurationMs` 200 → 150 → 100ms (`main_livekit.py`)

**Vorher:** `silenceDurationMs=200` (Vorsession)
**Zwischenstand:** `silenceDurationMs=150` (10.05.2026)
**Nachher:** `silenceDurationMs=100`

**Warum:** Minimale Stille nach User-Sprachende, bevor Gemini antwortet. Jede Reduktion um
50ms spart direkt 50ms aus Phase 1. `END_SENSITIVITY_HIGH` erkennt Sprechpausen zuverlässig,
sodass 100ms als untere Grenze praktikabel ist.

**Risiko:** Bei natürlichen Sprechpausen mitten im Satz könnte Gemini zu früh unterbrechen.
Wenn das beobachtet wird, auf 130ms erhöhen.

**Geschätzter Gewinn:** −100ms Phase 1 (gegenüber ursprünglichen 200ms)

---

### 4. `context_window_compression target_tokens` 1024 → 512 (`main_livekit.py`)

**Vorher:** `target_tokens=1024`
**Nachher:** `target_tokens=512`

**Warum:** Das Sliding-Window-Limit bestimmt, wie viel Kontext Gemini pro Inferenz verarbeitet.
Kleinerer Wert = weniger Tokens = schnellere Inferenz. Für einen Terminierungsanruf (10–20 Turns,
je ~20–50 Tokens Output) sind 512 Tokens Kontextfenster ausreichend — der System-Prompt (~180
Tokens) + letzter Exchange passen hinein.

**Geschätzter Gewinn:** −20 bis −60ms Phase 2 (weniger Kontext pro Inferenz)

---

### 5. `max_output_tokens` 200 → 120 (`main_livekit.py`)

**Vorher:** `max_output_tokens=200`
**Nachher:** `max_output_tokens=120`

**Warum:** Der Agent soll maximal 1–3 kurze Sätze sprechen. 200 Tokens lädt das Modell ein,
längere Antworten zu generieren (mehr Inferenzzeit, längere Turns). 120 Tokens erzwingen
Kürze und setzen ein früheres Ende der Generierungsphase. Typische Agent-Antworten in diesem
Use-Case liegen bei 20–60 Tokens.

**Geschätzter Gewinn:** −20 bis −60ms Phase 2 + kürzere Turns

---

### 6. Call-Start-Delay 1.5s → 1.0s (`main_livekit.py`)

**Vorher:** `asyncio.sleep(1.5)`
**Nachher:** `asyncio.sleep(1.0)`

**Warum:** Dieser Delay gibt dem AEC (Acoustic Echo Cancellation) Zeit zum Warmup.
`aec_warmup_duration=0.5s` + 0.5s Sicherheitspuffer = 1.0s ist ausreichend. 1.5s war
ein zu großzügiger Buffer.

**Betrifft:** Nur Gesprächsbeginn (kein Turn-Latenz-Effekt, aber wahrgenommene Latenz)

**Geschätzter Gewinn:** −500ms bei Session-Start

---

### 7. System-Prompt komprimiert (`config.py`)

**Vorher:** ~270 Tokens (geschätzt)
**Nachher:** ~185 Tokens (geschätzt) — ca. 30% Reduktion

**Was geändert wurde:**
- Redundante Formulierungen gekürzt ("wirklich nur etwa" → "wirklich nur")
- Füllwörter entfernt ("eigentlich", "Ihnen denn am besten")
- Einleitungen verkürzt ("freundlich beantworten:" → weggelassen)
- Format komprimiert (weniger Zeilenumbrüche)
- **Alle Inhalte erhalten:** Identität, Ton-Regeln, Begrüßung, Termin-Logik, Bestätigungstext,
  5 Einwandsantworten, Tool-Timing, Mailbox-Verhalten

**Warum:** Jeder Token im System-Prompt wird bei jeder Inferenz verarbeitet.
Weniger Tokens = schnellere Inferenz (server-seitig bei Gemini).

**Geschätzter Gewinn:** −10 bis −30ms Phase 2 (weniger Input-Tokens)

---

### 8. Monitor-Sends fire-and-forget (`main_livekit.py`) — 10.05.2026

**Vorher:** `await self._send_to_monitor(...)` in `_capture_loop`
**Nachher:** `asyncio.create_task(self._send_to_monitor(...))`

**Warum:** Der `await` blockierte die Audio-Capture-Loop. Wenn der Monitor-Server nicht
erreichbar war, wartete die Loop bis zu 1 Sekunde (HTTP-Timeout) alle ~1–2 Sekunden.
Mit `create_task` läuft Audio-Capture immer durch, unabhängig vom Monitor-Status.

**Geschätzter Gewinn:** Verhindert intermittente 1s-Pausen in der Audio-Pipeline

---

### 9. Duplikat-Name aus Trigger-Message entfernt (`main_livekit.py`) — 10.05.2026

**Vorher:** Partner-Name stand im System-Prompt UND im Trigger-Text
**Nachher:** Name nur im System-Prompt, Trigger generisch

**Warum:** Redundante Tokens in der ersten User-Message.

---

### 10. Retry-Sleep in `reporting.py` reduziert — 10.05.2026

**Vorher:** `time.sleep(5 * (attempt + 1))` — 5s, 10s
**Nachher:** `time.sleep(2 * (attempt + 1))` — 2s, 4s

**Betrifft:** Post-Call-Analyse bei API-Fehlern (kein Effekt auf Turn-Latenz)

---

## Gesamtschätzung kumulativer Gewinn

| Phase | Änderung | Geschätzter Effekt |
|---|---|---|
| Phase 1 | silenceDurationMs 200→100 | −100ms |
| Phase 1 | proactivity explizit (wenn vorher inaktiv) | −300–1000ms |
| Phase 2 | thinking_budget=0 (wenn vorher aktiv) | −0–500ms |
| Phase 2 | target_tokens 1024→512 | −20–60ms |
| Phase 2 | max_output_tokens 200→120 | −20–60ms |
| Phase 2 | System-Prompt −30% Tokens | −10–30ms |
| Start | Call-Start-Delay 1.5→1.0s | −500ms (einmalig) |
| Stabilität | Monitor fire-and-forget | keine Pausen mehr |

**Konservative Gesamtverbesserung:** −150–250ms pro Turn  
**Optimistische Gesamtverbesserung (proactivity war inaktiv):** −500–1800ms pro Turn

---

## Beobachtungen nach Deployment

*(Hier nach Testläufen ausfüllen)*

- Durchschnittliche E2E-Latenz vorher: ___ms
- Durchschnittliche E2E-Latenz nachher: ___ms
- Premature-Cutoffs bei silenceDurationMs=100: ja/nein
- proactivity-Status (war vorher aktiv?): ja/nein

---

## Code-Review Fixes — 11.05.2026

### K2 — Indentation-Bug Audio-Capture (`main_livekit.py:112-113`)
Zwei Zeilen (`logger.info` + `asyncio.create_task`) waren außerhalb des `if track.kind == AUDIO`-Blocks.
Bei bereits abonnierten Remote-Tracks beim `start()`-Aufruf wurde `_agent_task` mit dem
Partner-Track überschrieben → falsche Stereo-Aufnahmen. Zeilen entfernt.

### K3 — Blocking I/O in `finalize_session` auf Thread ausgelagert
`save_session_report` (File-I/O) und `email_service.send_*` (SMTP) liefen direkt im Event Loop
beim Hangup. Jetzt via `asyncio.to_thread()` → Event Loop bleibt während Cleanup frei.
`audio_recorder.save()` ebenfalls in Thread ausgelagert.

### K3b — Audio-Interleaving optimiert (vorallokierter Buffer)
Sample-Interleaving nutzte `bytearray.extend()` in einer Python-Schleife über alle Samples.
Bei 10-Minuten-Calls (~9,6M Samples) dauerte das mehrere Sekunden. Jetzt: `struct.pack_into`
mit vorallokiertem Buffer → ein einziger C-Aufruf.

### W2 — Mailbox-Check vor Farewell-Check
Mailboxen sprechen oft Verabschiedungsformeln ("Auf Wiederhören"). Farewell-Detection feuerte
vor Mailbox-Detection und verhinderte das sofortige Auflegen. Reihenfolge getauscht +
`return` nach Mailbox-Erkennung.

### W3 — Path-Traversal im Audio-Endpoint (`api_server.py`)
`os.path.realpath` + Präfix-Check stellt sicher dass die angefragte Datei wirklich im
`sessions/`-Verzeichnis liegt und kein `../` durchkommt.
