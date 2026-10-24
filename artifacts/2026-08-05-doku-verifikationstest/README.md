# Doku-Verifikationstest 05.08.2026 — Belegverzeichnis

**Was dieser Test war:** Prüfung der Kernfrage, ob `docs/` allein (ohne jede
weitere Quelle) einen MinerU-Lauf auf dem RunPod-QB-Endpoint `l79lebhinxaqjz`
trägt. Sechs Seiten Black-Scholes-Kursmaterial (kein PII), gefahren mit
`scripts/ocr_test.py` strikt nach dem Betriebshandbuch. Chronik und Befunde:
`docs/MinerU-RunPod-Geschichte-und-Entscheidungen.md`, Abschnitt 2.9.

## Inhalt

- **`blackscholes__-01/-02/-03/-10/-11/-12.png.response.json`** — die sechs
  VOLLEN `/status`-Antworten (Abruf-Regel: nichts verworfen). Jede trägt
  `output.debug.gpu` mit Kartenname und `compute_capability` — das ist der
  Beleg für den Blackwell-Befund (`NVIDIA RTX PRO 6000 Blackwell Server
  Edition MIG 1g.24gb`, `compute_capability "12.0"`, VLM-Backend erfolgreich)
  — sowie `output.mineru_version` (= `"unknown"`, Beleg für den
  Versionsfeld-Befund). `content_list`/`middle`/`images` sind vollständig
  enthalten (Base64-Bilder inklusive).
- **`blackscholes__-*.png.md`** — die Markdown-Extrakte daraus (nur
  Bequemlichkeit; die `.response.json` sind die Quelle).
- **`ledger.json`** — das Job-Ledger des Laufs (Job-IDs, Zustände,
  Submit-Zeiten).
- **`endpoint-snapshot.json`** — die REST-Antwort
  `GET /v1/endpoints/l79lebhinxaqjz?includeWorkers=true`, gezogen am
  05.08.2026 nach dem Test. Beleg für: `workersMax: 10` nach 24 Tagen
  Inaktivität (Auto-Downscale-Befund), `workersStandby: 3`,
  `flashboot: false`, Image-Tag `:17d4955b2`, `costPerHr: 0.49` an allen
  Worker-Einträgen, `RUNPOD_GPU_SIZE: "AMPERE_24,ADA_24"`.
  **Redigiert:** Die Worker-Env-Felder `RUNPOD_AI_API_KEY` und
  `RUNPOD_ENDPOINT_SECRET` sind durch `REDACTED` ersetzt, die Benutzer-ID des
  Kontos (`userId`, `consumerUserId`) durch `user_REDACTED` (Mineroo#2, vor dem
  ersten Push); sonst unverändert.
- **`user-snapshot.json`** — `runpodctl user` nach dem Test
  (Balance 5,1889, `currentSpendPerHr` 0; E-Mail und Benutzer-ID sind durch
  Platzhalter ersetzt, `konto@example.invalid` und `user_REDACTED`). Zusammen mit den im
  Chronik-Eintrag genannten Transkript-Werten (5,4642 vor / 5,2518
  unmittelbar nach dem Lauf) der Beleg für das Nachbuchungs-Verhalten der
  Abrechnung.

## Provenienz-Hinweise

- Die `.response.json`, `.md` und `ledger.json` wurden von der ERSTEN
  Fassung von `scripts/ocr_test.py` erzeugt. Die danach eingearbeiteten
  Script-Fixes (404-Retention als terminaler Zustand `lost:404-retention`;
  Ledger-Identität Dateiname + SHA-256) stammen aus einem Review NACH dem
  Lauf und haben die hier liegenden Artefakte nicht berührt — das alte
  Ledger-Format (ohne `sha256`/`path`-Felder) ist daran erkennbar.
- Die beiden Snapshots wurden ~35–40 Minuten nach dem Lauf gezogen, im
  selben Zustand (keine Requests dazwischen außer Status-/Listen-Lesezugriffen).
- Balance-Werte VOR dem Lauf existieren nur im Session-Transkript, nicht als
  Datei — im Chronik-Eintrag entsprechend gekennzeichnet.
