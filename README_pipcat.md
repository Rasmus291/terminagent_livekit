# README für Pipcat-Orchestrierung

Dieses Projekt verwendet eine Pipcat-Orchestrierungsstruktur.

## Struktur
- `pipcat.yml`: Definiert die Services und deren Abhängigkeiten.
- `main.py`: Hauptanwendung.
- `audio_handler.py`: Audioverarbeitung.
- `scratch/test_api.py`: Test-API.

## Nutzung
1. Stelle sicher, dass pipcat installiert ist (`pip install pipcat`).
2. Starte einen Service mit:
   
   pipcat run <service>

   Beispiel:
   
   pipcat run main

3. Weitere Services und Abhängigkeiten können in `pipcat.yml` ergänzt werden.

---

Für Fragen zur Orchestrierung oder Erweiterung der Struktur, bitte im Projekt nachfragen.