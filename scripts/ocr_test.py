#!/usr/bin/env python3
"""Mini-Testlauf MinerU auf RunPod — Doku-Verifikationstest (05.08.2026).

Fährt einzelne PNG-Seiten durch den QB-Endpoint l79lebhinxaqjz, exakt nach
docs/MinerU-RunPod-Betriebshandbuch.md:
  - /run + /status (nie /runsync nach Ruhe — 60-s-Fenster vs. ~8,5 min Kaltstart)
  - KEIN formats-Feld => alle vier Ausgaben (Abruf-Regel 14a)
  - transport inline, backend vlm-auto-engine
  - Job-ID VOR dem naechsten Submit ins Ledger (Write-Through, 9.2)
  - VOLLE Antwort persistieren, nicht nur markdown (Anforderung 11.3-10)
  - leeres Markdown ist ein eigener Zustand, kein Fehler (11.3-12)
  - kein JSON-null fuer optionale Felder (5.2)
  - /status-404 nach Ablauf der ~30-min-Retention ist TERMINAL (Zustand "lost"),
    kein Retry-Kandidat (5.4); andere HTTP-Fehler werden weiter gepollt
  - Ledger-Identitaet = Dateiname + Inhalts-Hash: gleicher Name mit anderem
    Inhalt wird neu submittet statt still uebersprungen
Aufruf: python3 ocr_test.py <bild.png> [bild2.png ...]
"""
import base64
import hashlib
import json
import pathlib
import sys
import time
import tomllib
import urllib.error
import urllib.request

EP = "l79lebhinxaqjz"
BASE = f"https://api.runpod.ai/v2/{EP}"
SCRATCH = pathlib.Path(__file__).parent
LEDGER = SCRATCH / "results" / "ledger.json"
RESULTS = SCRATCH / "results"
POLL_INTERVAL = 15          # s
JOB_TIMEOUT = 25 * 60       # s — deckt Kaltstart (~8,5 min) + Queue + Parse

KEY = tomllib.loads((pathlib.Path.home() / ".runpod" / "config.toml").read_text())["apikey"]
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def api(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=HDR)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def load_ledger():
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {}


def save_ledger(ledger):
    LEDGER.write_text(json.dumps(ledger, indent=2))


def submit(img_path):
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    payload = {"input": {
        "file_b64": b64,
        "backend": "vlm-auto-engine",
        "transport": "inline",
        # KEIN "formats"-Feld: Voreinstellung = alle vier Ausgaben (Abruf-Regel)
    }}
    resp = api(f"{BASE}/run", payload)
    return resp["id"], resp.get("status")


def main():
    imgs = [pathlib.Path(a) for i, a in enumerate(sys.argv) if i > 0]
    if not imgs:
        sys.exit("usage: ocr_test.py <bild.png> ...")
    for p in imgs:
        if not p.exists():
            sys.exit(f"fehlt: {p}")

    RESULTS.mkdir(exist_ok=True)
    ledger = load_ledger()

    # Phase 1: Submit — Adopt-statt-Resubmit: vorhandene lebende Job-ID weiterpollen.
    # Identitaet ist Dateiname + Inhalts-Hash: derselbe Name mit geaendertem Inhalt
    # (oder ein gleichnamiger anderer Pfad) wird als NEUE Einheit behandelt.
    for p in imgs:
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        key = p.name
        entry = ledger.get(key)
        if entry is not None and entry.get("sha256") != sha:
            log(f"{key}: Inhalts-Hash weicht vom Ledger ab (Ledger {entry.get('sha256')}, "
                f"Datei {sha}) — wird als neue Einheit submittet, alter Eintrag wird ueberschrieben")
            entry = None
        if entry is not None and entry.get("state") in ("submitted", "done", "empty"):
            log(f"{key}: bereits im Ledger ({entry['state']}, job {entry['job_id']}) — kein Resubmit")
            continue
        job_id, status = submit(p)
        ledger[key] = {"job_id": job_id, "state": "submitted", "path": str(p.resolve()),
                       "sha256": sha, "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_ledger(ledger)          # Write-Through VOR dem naechsten Submit
        log(f"{key}: submitted -> {job_id} ({status})")

    # Phase 2: Poll — Abholung mit Per-Job-Timeout; volle Antwort auf Platte
    t0 = time.time()
    open_jobs = {k: v for k, v in ledger.items() if v["state"] == "submitted"}
    while open_jobs and time.time() - t0 < JOB_TIMEOUT:
        for key in sorted(open_jobs):
            job_id = ledger[key]["job_id"]
            try:
                st = api(f"{BASE}/status/{job_id}")
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                if e.code == 404:
                    # Ergebnis-Retention (~30 min) abgelaufen oder Job unbekannt:
                    # TERMINAL — weiterpollen kann nie mehr etwas liefern (5.4).
                    ledger[key]["state"] = "lost:404-retention"
                    ledger[key]["lost_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    ledger[key]["lost_body"] = body
                    save_ledger(ledger)
                    log(f"{key}: /status 404 — Ergebnis nicht (mehr) abrufbar, Zustand lost:404-retention (kein Retry)")
                    del open_jobs[key]
                    continue
                log(f"{key}: /status HTTP {e.code} — {body}")
                continue
            s = st.get("status")
            if s in ("IN_QUEUE", "IN_PROGRESS", "RUNNING"):
                continue
            # Terminal: VOLLE Antwort persistieren
            out_file = RESULTS / f"{key}.response.json"
            out_file.write_text(json.dumps(st, indent=2, ensure_ascii=False))
            if s == "COMPLETED":
                results = (st.get("output") or {}).get("results") or []
                md = results[0].get("markdown", "") if results else ""
                md_file = RESULTS / f"{key}.md"
                md_file.write_text(md)
                ledger[key]["state"] = "done" if md.strip() else "empty"  # leer = eigener Zustand
                ledger[key]["exec_ms"] = st.get("executionTime")
                extra = "  [LEER — am Bild pruefen]" if not md.strip() else ""
                log(f"{key}: {s}, {st.get('executionTime')} ms, markdown {len(md)} Zeichen{extra}")
            else:
                ledger[key]["state"] = f"failed:{s}"
                log(f"{key}: TERMINAL {s} — volle Antwort in {out_file.name}")
            save_ledger(ledger)
            del open_jobs[key]
        if open_jobs:
            elapsed = int(time.time() - t0)
            log(f"warte... offen: {sorted(open_jobs)} ({elapsed}s)")
            time.sleep(POLL_INTERVAL)

    for key in open_jobs:
        log(f"{key}: TIMEOUT nach {JOB_TIMEOUT}s — Job-ID {ledger[key]['job_id']} bleibt im Ledger (adoptierbar)")

    log("fertig. Ledger: " + str(LEDGER))


if __name__ == "__main__":
    main()
