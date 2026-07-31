"""OCR all PDFs in B819_bis_Unit_3_Session_3 via the RunPod QB endpoint.

PDF page -> PNG (pdftoppm, 150 dpi) -> one job per page.

Why images and not split PDFs: pypdf's page extraction drags the whole document's
shared resource dictionary into every single-page file — 360 MB of source PDFs became
4.1 GB of "single pages". pdftoppm renders each page independently: ~278 KB per page,
~760 MB for the whole corpus. MinerU takes images as first-class input (magic-byte
detection -> single-page PDF internally).

Other constraints, all read from the docs rather than assumed:
  * /run payload cap 10 MB — a 278 KB PNG is ~370 KB as base64. Never close to it.
  * /run results are retained only 30 MINUTES -> submission and collection OVERLAP
    in a rolling window. Submit-all-then-collect would let early results expire.
  * The worker repo retries NOTHING -> retries are ours.
  * Backend vlm-auto-engine: measured on this corpus against hybrid-auto-engine
    (effort=high). Hybrid dropped 13 of 41 inline formulas and silently lost
    subscripts (R_f -> R) while being 2.2x slower. Pure VLM wins on maths.
  * Each page's markdown is written the moment it lands -> a crash resumes.
"""
import base64
import json
import pathlib
import subprocess
import time
import tomllib
import urllib.request

ENDPOINT = "l79lebhinxaqjz"
BASE = f"https://api.runpod.ai/v2/{ENDPOINT}"
SRC = pathlib.Path("/Users/jurgen/AgentRuns/LetsDoUni/B819_bis_Unit_3_Session_3")
OUT = SRC / "ocr"
IMGS = pathlib.Path("/Users/jurgen/.claude/jobs/0fba4d0a/tmp/pageimgs")
DPI = 150
IN_FLIGHT = 120
MIN_BALANCE = 0.50

KEY = tomllib.loads((pathlib.Path.home() / ".runpod" / "config.toml").read_text())["apikey"]
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

OUT.mkdir(exist_ok=True)
IMGS.mkdir(parents=True, exist_ok=True)


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


def balance():
    try:
        return float(json.loads(subprocess.run(
            ["runpodctl", "user"], capture_output=True, text=True).stdout)["clientBalance"])
    except Exception:
        return None


def post(payload):
    req = urllib.request.Request(BASE + "/run", data=json.dumps(payload).encode(),
                                 headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["id"]


def status(jid):
    req = urllib.request.Request(BASE + f"/status/{jid}", headers=HDR, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# Die fuenf Konzern-Geschaeftsberichte sind Fallstudien-Rohmaterial, kein Kursinhalt:
# 1801 der 2733 Seiten, zwei Drittel der Kosten, und sie haben ohnehin eine makellose
# Textebene (3500-4500 Zeichen/Seite). Jurgen: "obviously die jahresberichte nicht".
EXCLUDE = {
    "Santander Annual Report 2021",
    "BP Annual Report and Form 20-F 2021",
    "Rolls-Royce annual report 2021",
    "Rolls-Royce 2022 annual report",
    "Unilever Annual Report on Form 20-F 2021",
}

# ---------- Phase 0: jede PDF-Seite als PNG rendern ----------
pdfs = [p for p in sorted(SRC.glob("*.pdf")) if p.stem not in EXCLUDE]
log(f"{len(pdfs)} PDFs (ausgeschlossen: {len(EXCLUDE)} Geschaeftsberichte)")
for pdf in pdfs:
    stem = pdf.stem
    marker = IMGS / f".done__{stem}"
    if marker.exists():
        continue
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(DPI), str(pdf), str(IMGS / f"{stem}__")],
        check=True,
    )
    marker.touch()
    log(f"gerendert: {stem}")

tasks = sorted(IMGS.glob("*__*.png"))
log(f"{len(tasks)} Seitenbilder aus {len(pdfs)} PDFs "
    f"({sum(t.stat().st_size for t in tasks)/1e6:.0f} MB)")

todo = [t for t in tasks if not (OUT / f"{t.stem}.md").exists()]
log(f"{len(tasks) - len(todo)} bereits vorhanden, {len(todo)} zu tun")

# ---------- rollendes Fenster: einreichen + einsammeln ----------
pending = {}
done = 0
failed = {}
last_report = time.time()

while todo or pending:
    while todo and len(pending) < IN_FLIGHT:
        img = todo.pop(0)
        payload = {"input": {"file_b64": base64.b64encode(img.read_bytes()).decode(),
                             "backend": "vlm-auto-engine",
                             "formats": ["markdown"], "transport": "inline"}}
        try:
            pending[post(payload)] = img.stem
        except Exception as e:
            failed[img.stem] = f"submit: {e}"

    for jid in list(pending):
        try:
            r = status(jid)
        except Exception:
            continue
        st = r.get("status")
        if st in ("IN_QUEUE", "IN_PROGRESS"):
            continue
        name = pending.pop(jid)
        if st == "COMPLETED":
            md = ""
            try:
                md = r["output"]["results"][0]["markdown"] or ""
            except Exception:
                pass
            (OUT / f"{name}.md").write_text(md)
            done += 1
        else:
            failed[name] = f"{st}: {r.get('error')}"

    if time.time() - last_report > 30:
        b = balance()
        log(f"fertig={done}  in_flight={len(pending)}  offen={len(todo)}  fehler={len(failed)}  balance={b}")
        last_report = time.time()
        if b is not None and b < MIN_BALANCE:
            log(f"ABBRUCH: Guthaben {b} unter {MIN_BALANCE}")
            break
    time.sleep(3)

# ---------- zusammenfuegen: ein Markdown pro PDF, Seiten in Reihenfolge ----------
for pdf in pdfs:
    stem = pdf.stem
    parts = sorted(OUT.glob(f"{stem}__*.md"))
    if not parts:
        continue
    (OUT / f"{stem}.md").write_text("\n\n".join(p.read_text() for p in parts))

log(f"FERTIG: {done} Seiten geschrieben, {len(failed)} Fehler")
for k, v in failed.items():
    log(f"  FEHLER {k}: {v}")
