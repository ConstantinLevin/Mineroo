"""Schickt die Aufnahme zur Bildanalyse ueber den Client des Pakets an den echten Endpoint.

Aufruf (mit diesem Repository auf dem PYTHONPATH): abschicken.py MATERIAL ZIELORDNER ENDPOINT_ID
Ein Job mit `material/bild_seite.pdf`: `RunPodClient.submit` mit den Lese-Einstellungen
der Voreinstellung (`advanced`, `ocr`, Bildanalyse an, `tarball_b64`), dann
`await_result`; die volle /status-Antwort geht nach ZIEL/bild_seite.response.json.
Das Guthaben wird davor und danach gelesen und ausgegeben.
"""
import asyncio
import base64
import json
import pathlib
import sys
import time

from mineru_ocr.config import Settings
from mineru_ocr.runpod import RunPodClient

material, ziel, endpoint = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
ziel.mkdir(parents=True, exist_ok=True)


async def los() -> None:
    einstellung = Settings.load(db_dsn="ungenutzt", endpoint_id=endpoint)
    async with RunPodClient(einstellung) as client:
        print("GUTHABEN_VORHER", await client.balance(), flush=True)
        if (ziel / "bild_seite.response.json").exists():
            print("SCHON_DA", flush=True)
            return
        beginn = time.monotonic()
        job = await client.submit(
            file_b64=base64.b64encode((material / "bild_seite.pdf").read_bytes()).decode("ascii"),
            tier=einstellung.tier,
            ocr_mode=einstellung.ocr_mode,
            image_analysis=einstellung.image_analysis,
            transport=einstellung.transport,
        )
        print("ABGESCHICKT", job, flush=True)
        antwort = await client.await_result(job)
        (ziel / "bild_seite.response.json").write_text(json.dumps(antwort, ensure_ascii=False))
        print("FERTIG", job, antwort.get("status"), round(time.monotonic() - beginn), "s", flush=True)
        print("GUTHABEN_NACHHER", await client.balance(), flush=True)


asyncio.run(los())
