"""Schickt die Aufnahme-Jobs ueber den Client des Pakets an den echten Endpoint.

Aufruf (mit diesem Repository auf dem PYTHONPATH): abschicken.py JOBS_ORDNER ZIELORDNER ENDPOINT_ID
Je Job: `RunPodClient.submit` mit genau den Feldern der Job-Datei, dann
`await_result`; die volle /status-Antwort geht nach ZIEL/<name>.response.json.
"""
import asyncio
import json
import pathlib
import sys
import time

from mineru_ocr.config import Settings
from mineru_ocr.runpod import RunPodClient

jobs, ziel, endpoint = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
ziel.mkdir(parents=True, exist_ok=True)


async def einer(client: RunPodClient, datei: pathlib.Path) -> None:
    name = datei.name.removesuffix(".job.json")
    if (ziel / f"{name}.response.json").exists():
        print("SCHON_DA", name, flush=True)
        return
    e = json.loads(datei.read_text())["input"]
    beginn = time.monotonic()
    job = await client.submit(file_b64=e["file_b64"], tier=e["tier"], ocr_mode=e["ocr_mode"],
                              image_analysis=e["image_analysis"], transport=e["transport"])
    print("ABGESCHICKT", name, job, flush=True)
    antwort = await client.await_result(job)
    (ziel / f"{name}.response.json").write_text(json.dumps(antwort, ensure_ascii=False))
    print("FERTIG", name, job, antwort.get("status"), round(time.monotonic() - beginn), "s", flush=True)


async def alle() -> None:
    einstellung = Settings.load(db_dsn="ungenutzt", endpoint_id=endpoint)
    async with RunPodClient(einstellung) as client:
        await asyncio.gather(*(einer(client, d) for d in sorted(jobs.glob("*.job.json"))))
    print("ALLE_FERTIG", flush=True)


asyncio.run(alle())
