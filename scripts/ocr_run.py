"""OCR all 593 cropped page images via the RunPod QB endpoint, write markdown
into the `ocr` field of ocr_values.json.

Design notes (all learned the hard way today):
  * ONE job per image, file_b64 (each image <= 1.4 MB -> ~1.9 MB b64, cap is 20 MB).
  * backend vlm-auto-engine  (pure VLM; hybrid's default silently drops content).
  * transport inline, formats [markdown]  -> markdown comes back in the response.
  * NEVER send JSON null for optional fields; omit them instead.
  * The worker repo retries NOTHING -> retries are ours.
  * Submit everything up front so the workers never idle (idle = re-warm = paid).
  * Save incrementally: a crash must not lose completed pages.
  * Balance brake: stop if the account runs low.
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
ROOT = pathlib.Path("/Users/jurgen/AgentRuns/B219Buch")
VALUES = ROOT / "ocr_values.json"
STATE = pathlib.Path("/Users/jurgen/.claude/jobs/0fba4d0a/tmp/ocr_jobs.json")
MIN_BALANCE = 0.15

KEY = tomllib.loads((pathlib.Path.home() / ".runpod" / "config.toml").read_text())["apikey"]
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def balance():
    out = subprocess.run(["runpodctl", "user"], capture_output=True, text=True).stdout
    try:
        return float(json.loads(out)["clientBalance"])
    except Exception:
        return None


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), headers=HDR, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def get(path):
    req = urllib.request.Request(BASE + path, headers=HDR, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


records = json.loads(VALUES.read_text())
todo = [i for i, rec in enumerate(records) if rec.get("ocr") is None]
log(f"{len(records)} Records, davon {len(todo)} ohne ocr")

jobs = json.loads(STATE.read_text()) if STATE.exists() else {}

# ---------- Phase 1: alles einreichen ----------
for n, idx in enumerate(todo, 1):
    if str(idx) in jobs:
        continue
    if n % 50 == 0:
        b = balance()
        log(f"eingereicht {n}/{len(todo)}   balance={b}")
        if b is not None and b < MIN_BALANCE:
            log(f"ABBRUCH beim Einreichen: Guthaben {b} unter {MIN_BALANCE}")
            break
    img = pathlib.Path(records[idx]["cropped"])
    b64 = base64.b64encode(img.read_bytes()).decode()
    payload = {
        "input": {
            "file_b64": b64,
            "backend": "vlm-auto-engine",
            "formats": ["markdown"],
            "transport": "inline",
        }
    }
    for attempt in range(4):
        try:
            jobs[str(idx)] = post("/run", payload)["id"]
            break
        except Exception as e:
            if attempt == 3:
                log(f"  idx={idx} EINREICHEN GESCHEITERT: {e}")
            else:
                time.sleep(2 * (attempt + 1))
    STATE.write_text(json.dumps(jobs))

log(f"eingereicht: {len(jobs)} Jobs")

# ---------- Phase 2: einsammeln ----------
pending = dict(jobs)
done = 0
failed = {}
last_save = time.time()

while pending:
    for idx, jid in list(pending.items()):
        try:
            r = get(f"/status/{jid}")
        except Exception:
            continue
        st = r.get("status")
        if st in ("IN_QUEUE", "IN_PROGRESS"):
            continue
        pending.pop(idx, None)
        if st == "COMPLETED":
            try:
                md = r["output"]["results"][0]["markdown"]
            except Exception:
                md = None
            if md:
                records[int(idx)]["ocr"] = md
                done += 1
            else:
                failed[idx] = "leeres markdown"
        else:
            failed[idx] = f"{st}: {r.get('error')}"

    if time.time() - last_save > 20:
        VALUES.write_text(json.dumps(records, ensure_ascii=False, indent=2))
        b = balance()
        log(f"fertig={done}  offen={len(pending)}  fehler={len(failed)}  balance={b}")
        last_save = time.time()
        if b is not None and b < 0.05:
            log("ABBRUCH: Guthaben erschoepft")
            break
    if pending:
        time.sleep(10)

VALUES.write_text(json.dumps(records, ensure_ascii=False, indent=2))
still_null = sum(1 for rec in records if rec.get("ocr") is None)
log(f"FERTIG: {done} Seiten geschrieben, {len(failed)} Fehler, {still_null} Records weiterhin ohne ocr")
if failed:
    log("Fehler im Detail:")
    for k, v in failed.items():
        log(f"  idx={k}  {v}")
