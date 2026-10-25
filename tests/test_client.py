"""Die Fassade — Preflight, Einreihen, Warten, Nachsehen."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import NullConnectionPool

from mineru_ocr import client as client_modul
from mineru_ocr import config, core, db, tasks
from mineru_ocr.client import OCRClient
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, write_terminal_async
from mineru_ocr.errors import (
    ConfigDriftError,
    ConfigurationError,
    DatabaseError,
    DocumentNotFound,
    TooLargeError,
    UnsupportedTypeError,
)
from mineru_ocr.wrapper import OCRWrapper

pytestmark = pytest.mark.db

PNG_KOPF = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class RunPodAntworten:
    """Vorgegebene RunPod-Antworten, ohne Netz.

    Ersetzt `_runpod_fragen` — die EINE Stelle, an der der Client mit RunPod
    spricht. Das ist die Naht, und dass es nur eine gibt, ist der Grund, warum
    dieser Test ohne HTTP auskommt.
    """

    def __init__(
        self,
        *,
        endpoint: dict[str, Any] | None = None,
        marke: str | None = ":neu12345",
        kontostand: float = 5.0,
        worker: list[dict[str, Any]] | None = None,
    ) -> None:
        self.endpoint = endpoint if endpoint is not None else _guter_endpoint()
        self.marke = marke
        self.kontostand = kontostand
        self.worker = worker if worker is not None else []
        self.aufrufe: list[str] = []

    def __call__(self, settings: Settings, was: Any) -> Any:
        # Welche Methode gefragt wurde, steht im Namen der uebergebenen
        # Funktion nicht — also wird sie gegen ein Doppel ausgefuehrt, das
        # mitschreibt.
        return was(self)

    # Die vier Methoden, die der Client benutzt.
    async def endpoint_(self) -> dict[str, Any]:  # pragma: no cover — s. unten
        raise AssertionError

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


class Zugang:
    """Das Doppel, das `_runpod_fragen` an die uebergebene Funktion reicht."""

    def __init__(self, antworten: RunPodAntworten) -> None:
        self._a = antworten

    async def endpoint(self) -> dict[str, Any]:
        self._a.aufrufe.append("endpoint")
        return self._a.endpoint

    async def image_tag(self) -> str | None:
        self._a.aufrufe.append("image_tag")
        return self._a.marke

    async def balance(self) -> float:
        self._a.aufrufe.append("balance")
        return self._a.kontostand

    async def account(self) -> dict[str, Any]:
        self._a.aufrufe.append("account")
        return {"clientBalance": self._a.kontostand}

    async def workers(self) -> list[dict[str, Any]]:
        self._a.aufrufe.append("workers")
        return self._a.worker


def _guter_endpoint(**abweichungen: Any) -> dict[str, Any]:
    """Eine Endpoint-Konfiguration in der Form des konservierten Schnappschusses."""
    grund = {
        "id": "testendpoint",
        "name": "mineru-runpod",
        "workersMin": 0,
        "workersMax": 10,
        "idleTimeout": 60,
        "executionTimeoutMs": 600000,
        "templateId": "5wv9rko6zv",
        "gpuTypeIds": ["NVIDIA GeForce RTX 4090"],
        "workers": [],
    }
    grund.update(abweichungen)
    return grund


@pytest.fixture
def runpod(monkeypatch: pytest.MonkeyPatch) -> RunPodAntworten:
    """Haengt das RunPod-Doppel an die einzige Stelle, die mit RunPod spricht."""
    antworten = RunPodAntworten()

    def ersatz(settings: Settings, was: Any) -> Any:
        # UEBER `_synchron_ausfuehren` und nicht ueber `asyncio.run`: Sonst
        # bricht das Doppel in einem asynchronen Test genau an der Stelle, die
        # der Code eigens behandelt („asyncio.run() cannot be called from a
        # running event loop"). Ein Doppel, das die Umgebung schlechter
        # nachbildet als die Wirklichkeit, prueft die falsche Sache.
        return client_modul._synchron_ausfuehren(was(Zugang(antworten)))

    monkeypatch.setattr(client_modul, "_runpod_fragen", ersatz)
    return antworten


@pytest.fixture
def offener_client(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> Iterator[OCRClient]:
    """Ein betretener Client OHNE Preflight — den prueft eine eigene Gruppe."""
    with OCRClient(db_dsn=_test_datenbank, api_key="erfunden", preflight=False) as c:
        yield c


def _png(name: str) -> bytes:
    """Bytes, die als PNG erkannt werden und je Name ein eigenes Dokument sind."""
    return PNG_KOPF + name.encode()


def _hash(inhalt: bytes) -> str:
    return hashlib.sha256(inhalt).hexdigest()


def _abschicken(client: OCRClient, name: str) -> str:
    """Schickt ein PNG ab und gibt seinen Hash."""
    return str(client.ocr_start(_png(name)))


def _zaehlen(conn: psycopg.Connection[Any], tabelle: str) -> int:
    zeile = conn.execute(f"SELECT count(*) FROM {tabelle}").fetchone()
    assert zeile is not None
    return int(zeile[0])


def _worker_am_leben(conn: psycopg.Connection[Any]) -> None:
    conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")


def _lebendigkeit_unlesbar(monkeypatch: pytest.MonkeyPatch) -> None:
    """`db.worker_heartbeats` scheitert, wie es bei einer fehlenden oder
    umgebauten Worker-Tabelle oder einer weggebrochenen Verbindung tut."""

    def kaputt(conn: psycopg.Connection[Any]) -> Any:
        try:
            raise OSError("verbindung weg")
        except OSError as ursache:
            raise DatabaseError("procrastinate_workers sieht anders aus") from ursache

    monkeypatch.setattr(db, "worker_heartbeats", kaputt)


def _warnungen_des_clients(caplog: pytest.LogCaptureFixture) -> int:
    """Wie viele Warnungen der Logger des Clients bisher aufgezeichnet hat."""
    return len([e for e in caplog.records if e.name == "mineru_ocr.client"])


def _jobs_zaehlen(conn: psycopg.Connection[Any], sha256: str) -> int:
    """Wie viele Procrastinate-Jobs zu diesem Dokument eingereiht sind."""
    zeile = conn.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE queueing_lock = %s",
        (sha256,),
    ).fetchone()
    assert zeile is not None
    return int(zeile[0])


# --- Preflight ------------------------------------------------------------


def test_preflight_mutiert_nichts(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Kein Schreibzugriff auf den Endpoint, kein Anlegen, kein Aendern.

    Geprueft daran, welche Methoden der Preflight ueberhaupt aufruft: Es sind
    ausschliesslich lesende. Es gibt in diesem Paket ohnehin keinen Weg,
    `workersMax` zu setzen oder ein Template zu aendern — nicht als
    Voreinstellung, sondern gar nicht (Plan 8, Regel 10).
    """
    _worker_am_leben(db_conn)
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False
    ) as klient:
        befund = klient.preflight()

    assert set(runpod.aufrufe) <= {"endpoint", "image_tag", "balance"}
    assert befund["drift"] == 0


GEHEIMNIS = "geheim-nicht-ausgeben"
"""Steht in der Umgebung jedes Test-Workers wie `RUNPOD_AI_API_KEY` in der echten:
Der Befund darf es nie zeigen."""


def _worker(
    kennung: str,
    *,
    image: str | None = ":neu12345",
    env: dict[str, str] | None = None,
    status: str = "RUNNING",
) -> dict[str, Any]:
    """Ein Worker in der Form des konservierten Schnappschusses: `id`,
    `imageName`, `desiredStatus` und eine `env`, die auch Geheimnisse traegt."""
    umgebung = {
        "MINERU_MODEL_SOURCE": "local",
        "MINERU_MAX_CONCURRENCY": "1",
        "RUNPOD_AI_API_KEY": GEHEIMNIS,
    }
    umgebung.update(env or {})
    eintrag: dict[str, Any] = {
        "id": kennung,
        "desiredStatus": status,
        "env": umgebung,
    }
    if image is not None:
        eintrag["imageName"] = image
    return eintrag


def _pruefen(
    dsn: str, *, strict: bool = False
) -> dict[str, Any]:
    with OCRClient(
        db_dsn=dsn, api_key="erfunden", preflight=False, strict=strict
    ) as klient:
        return klient.preflight()


def _befund(befund: dict[str, Any], punkt: str) -> dict[str, Any]:
    treffer: list[dict[str, Any]] = [
        b for b in befund["checks"] if b["punkt"] == punkt
    ]
    assert treffer, (
        f"Der Preflight meldet keinen Punkt {punkt!r}; er meldet "
        f"{[b['punkt'] for b in befund['checks']]}"
    )
    return treffer[0]


def test_ohne_worker_ist_das_laufende_nicht_pruefbar(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Laeuft und lief kein Worker, sind Umgebung und Worker-Image NICHT pruefbar —
    und der Befund sagt das, statt „in Ordnung" zu melden. Genau darin liegt der
    Wert dieser Pruefung."""
    _worker_am_leben(db_conn)
    befund = _pruefen(_test_datenbank)

    for punkt in ("env:MINERU_MAX_CONCURRENCY", "env:MINERU_MODEL_SOURCE", "worker_image"):
        assert _befund(befund, punkt)["bewertung"] == "nicht_pruefbar", punkt


def test_passende_umgebung_ist_in_ordnung(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Gegenprobe: Mit einem Worker, dessen Variablen den Sollwerten
    entsprechen, sind sie in Ordnung."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(workers=[_worker("w1")])
    befund = _pruefen(_test_datenbank)

    modell = _befund(befund, "env:MINERU_MODEL_SOURCE")
    assert modell["bewertung"] == "in_ordnung"
    assert modell["soll"] == "local"
    assert modell["ist"] == {"w1": "local"}
    assert _befund(befund, "env:MINERU_MAX_CONCURRENCY")["bewertung"] == "in_ordnung"
    # Ohne eingestellten Tag ist das Image kein Vergleich wert, auch mit Workern.
    assert _befund(befund, "worker_image")["bewertung"] == "nicht_pruefbar"


def test_umgebung_wird_nach_wert_geprueft_nicht_nach_vorhandensein(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Variable ist da, aber mit dem falschen Wert: Abweichung. Vorher hiess
    das „MINERU_MAX_CONCURRENCY vorhanden" und war in Ordnung."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", env={"MINERU_MAX_CONCURRENCY": "4"})]
    )
    befund = _pruefen(_test_datenbank)
    assert _befund(befund, "env:MINERU_MAX_CONCURRENCY")["bewertung"] == "abweichung"
    assert befund["drift"] == 1

    with pytest.raises(ConfigDriftError):
        _pruefen(_test_datenbank, strict=True)


def test_fehlende_variable_ist_eine_abweichung(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Eine Variable, die der Worker gar nicht traegt, ist nicht „nicht pruefbar":
    Die Umgebung ist bekannt, die Variable fehlt in ihr."""
    _worker_am_leben(db_conn)
    arbeiter = _worker("w1")
    del arbeiter["env"]["MINERU_MODEL_SOURCE"]
    runpod.endpoint = _guter_endpoint(workers=[arbeiter])
    befund = _pruefen(_test_datenbank)

    modell = _befund(befund, "env:MINERU_MODEL_SOURCE")
    assert modell["bewertung"] == "abweichung"
    assert modell["ist"] == {"w1": None}


def test_jeder_worker_wird_geprueft_auch_beendete(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Antwort fuehrt die Umgebung der zuletzt gelaufenen Worker, auch der
    beendeten. Geprueft wird an JEDEM, nicht nur am ersten: Der erste ist
    richtig, der zweite (beendet) falsch — das ist eine Abweichung."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(
        workers=[
            _worker("w1"),
            _worker("w2", env={"MINERU_MODEL_SOURCE": "huggingface"}, status="EXITED"),
        ]
    )
    befund = _pruefen(_test_datenbank)

    modell = _befund(befund, "env:MINERU_MODEL_SOURCE")
    assert modell["bewertung"] == "abweichung"
    assert modell["ist"] == {"w1": "local", "w2": "huggingface"}


def test_worker_ohne_umgebung_macht_den_befund_unsicher(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Fuehrt die Antwort fuer einen Worker keine Umgebung, ist an ihm nichts
    festzustellen. Die uebrigen sind in Ordnung — gesamt kann der Befund dann
    aber nicht „in Ordnung" lauten."""
    _worker_am_leben(db_conn)
    ohne = _worker("w2")
    del ohne["env"]
    runpod.endpoint = _guter_endpoint(workers=[_worker("w1"), ohne])
    befund = _pruefen(_test_datenbank)

    assert _befund(befund, "env:MINERU_MAX_CONCURRENCY")["bewertung"] == "nicht_pruefbar"


def test_offener_sollwert_wird_berichtet_aber_nicht_bewertet(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Fuer den GPU-Speicheranteil gibt es noch keinen gemessenen Sollwert. Der
    Ist-Wert steht trotzdem im Befund; bewertet wird er nicht."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", env={"MINERU_VIRTUAL_VRAM_SIZE": "12"})]
    )
    befund = _pruefen(_test_datenbank)

    vram = _befund(befund, "env:MINERU_VIRTUAL_VRAM_SIZE")
    assert vram["bewertung"] == "nicht_pruefbar"
    assert vram["soll"] is None
    assert vram["ist"] == {"w1": "12"}


def test_sollwert_der_umgebungsvariable_kommt_aus_der_konfiguration(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Der Sollwert des GPU-Speicheranteils steht in der Konfigurationsdatei; was
    der Worker abweichend fuehrt, ist eine Abweichung."""
    datei = tmp_path / "config.toml"
    datei.write_text('[endpoint_env]\nMINERU_VIRTUAL_VRAM_SIZE = "12"\n')
    monkeypatch.setenv("MINERU_OCR_CONFIG", str(datei))
    _worker_am_leben(db_conn)

    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", env={"MINERU_VIRTUAL_VRAM_SIZE": "12"})]
    )
    assert (
        _befund(_pruefen(_test_datenbank), "env:MINERU_VIRTUAL_VRAM_SIZE")["bewertung"]
        == "in_ordnung"
    )

    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", env={"MINERU_VIRTUAL_VRAM_SIZE": "6"})]
    )
    assert (
        _befund(_pruefen(_test_datenbank), "env:MINERU_VIRTUAL_VRAM_SIZE")["bewertung"]
        == "abweichung"
    )


def test_geheimnisse_der_worker_bleiben_aus_dem_befund(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Umgebung eines echten Workers traegt `RUNPOD_AI_API_KEY` im Klartext
    (am 08.08.2026 stand er in der Konsolenausgabe). Gezeigt werden nur die
    geprueften Variablen."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(workers=[_worker("w1")])
    befund = _pruefen(_test_datenbank)

    assert GEHEIMNIS not in repr(befund)
    assert "RUNPOD_AI_API_KEY" not in repr(befund)


def test_template_image_wird_gegen_den_erwarteten_tag_geprueft(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Der erwartete Tag ist eine Einstellung. Ein anderer ist eine Warnung —
    mit anderem Image kann man arbeiten, man soll es nur wissen — und kein
    Abbruch."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")

    runpod.marke = "registry.runpod.net/konto-repo-dockerfile:neu12345"
    befund = _pruefen(_test_datenbank, strict=True)
    assert _befund(befund, "image_tag")["bewertung"] == "in_ordnung"
    assert _befund(befund, "image_tag")["soll"] == "neu12345"

    runpod.marke = "registry.runpod.net/konto-repo-dockerfile:17d4955b2"
    befund = _pruefen(_test_datenbank, strict=True)  # wirft NICHT
    assert _befund(befund, "image_tag")["bewertung"] == "warnung"
    assert befund["drift"] == 0


def test_ohne_erwarteten_tag_ist_das_image_nicht_pruefbar(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Kein Tag eingestellt: Das Image wird berichtet, aber nicht fuer richtig
    gehalten. Dasselbe gilt, wenn das Template keinen Tag ausweist."""
    _worker_am_leben(db_conn)
    befund = _pruefen(_test_datenbank)
    assert _befund(befund, "image_tag")["bewertung"] == "nicht_pruefbar"
    assert _befund(befund, "image_tag")["ist"] == ":neu12345"


def test_unbekanntes_template_image_ist_nicht_pruefbar(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")
    runpod.marke = None
    befund = _pruefen(_test_datenbank)
    assert _befund(befund, "image_tag")["bewertung"] == "nicht_pruefbar"


def test_image_jedes_workers_wird_gegen_den_erwarteten_tag_geprueft(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Das Template kann stimmen und ein Worker trotzdem noch das alte Image
    fahren (er wurde vor dem Build gestartet). Das steht im Befund, mit
    Kennung."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")

    runpod.endpoint = _guter_endpoint(
        workers=[
            _worker("w1", image="registry.runpod.net/k-r-dockerfile:neu12345"),
            _worker("w2", image="registry.runpod.net/k-r-dockerfile:17d4955b2", status="EXITED"),
        ]
    )
    befund = _pruefen(_test_datenbank, strict=True)  # warnung, kein Abbruch

    punkt = _befund(befund, "worker_image")
    assert punkt["bewertung"] == "warnung"
    assert punkt["ist"] == {"w1": "neu12345", "w2": "17d4955b2"}

    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", image="registry.runpod.net/k-r-dockerfile:neu12345")]
    )
    assert _befund(_pruefen(_test_datenbank), "worker_image")["bewertung"] == "in_ordnung"


def test_worker_ohne_image_angabe_ist_nicht_pruefbar(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")
    runpod.endpoint = _guter_endpoint(workers=[_worker("w1", image=None)])
    assert _befund(_pruefen(_test_datenbank), "worker_image")["bewertung"] == "nicht_pruefbar"


def test_rechenzentren_muessen_alle_in_der_zulaessigen_liste_liegen(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Nennt die Endpoint-Antwort Rechenzentren, muessen ALLE zulaessig sein (EWR).
    Eines ausserhalb ist eine Abweichung, auch wenn die uebrigen stimmen — daran
    haengt, ob personenbezogene Daten die EU verlassen koennen."""
    _worker_am_leben(db_conn)

    runpod.endpoint = _guter_endpoint(dataCenterIds=["EU-RO-1", "EUR-IS-1"])
    befund = _pruefen(_test_datenbank, strict=True)
    zentren = _befund(befund, "datacenters")
    assert zentren["bewertung"] == "in_ordnung"
    assert zentren["ist"] == ["EU-RO-1", "EUR-IS-1"]
    assert "EU-RO-1" in zentren["soll"]

    runpod.endpoint = _guter_endpoint(dataCenterIds=["EU-RO-1", "US-CA-2"])
    befund = _pruefen(_test_datenbank)
    assert _befund(befund, "datacenters")["bewertung"] == "abweichung"
    with pytest.raises(ConfigDriftError):
        _pruefen(_test_datenbank, strict=True)


def test_rechenzentren_sind_ohne_angabe_der_antwort_nicht_pruefbar(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Frage bleibt OFFEN, wenn die Antwort keine Rechenzentren nennt — die
    Aufnahme vom 05.08.2026 hat kein `dataCenterIds`.

    WAS FRUEHER AN DIESER STELLE FALSCH WAR: Die Pruefung las `dataCenterIds`
    ODER, ersatzweise, `gpuTypeIds` und meldete GPU-Typen
    (`['NVIDIA RTX A5000', …]`) unter der Ueberschrift „EU". Die Vorrichtung
    bildet den echten Schnappschuss ab (mit `gpuTypeIds`, ohne
    `dataCenterIds`); der richtige Befund ist `nicht_pruefbar`.
    """
    _worker_am_leben(db_conn)
    befund = _pruefen(_test_datenbank, strict=True)
    zentren = _befund(befund, "datacenters")
    assert zentren["bewertung"] == "nicht_pruefbar"
    assert zentren["ist"] is None


def test_image_ohne_tag_ist_nicht_pruefbar_auch_mit_registry_port(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`registry:5000/repo` hat einen Port und keinen Tag; die Zahl nach dem
    Doppelpunkt ist nicht der Tag."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "5000/k-r")
    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", image="registry.runpod.net:5000/k-r")]
    )
    punkt = _befund(_pruefen(_test_datenbank), "worker_image")
    assert punkt["bewertung"] == "nicht_pruefbar"
    assert punkt["ist"] == {"w1": None}


def test_image_mit_leerem_tag_ist_nicht_pruefbar(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`repo:` hat einen Doppelpunkt und keinen Tag — das ist kein Tag, der
    verglichen werden koennte."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")
    runpod.endpoint = _guter_endpoint(workers=[_worker("w1", image="registry.runpod.net/k-r:")])
    punkt = _befund(_pruefen(_test_datenbank), "worker_image")
    assert punkt["bewertung"] == "nicht_pruefbar"
    assert punkt["ist"] == {"w1": None}


def test_leere_rechenzentrumsliste_ist_nicht_pruefbar(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Eine leere Liste ist keine Aussage — „alle liegen in der Liste" waere
    ueber nichts trivial wahr."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(dataCenterIds=[])
    assert _befund(_pruefen(_test_datenbank), "datacenters")["bewertung"] == "nicht_pruefbar"


def test_flashboot_wird_berichtet(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    _worker_am_leben(db_conn)

    runpod.endpoint = _guter_endpoint(flashboot=True)
    einmal = _befund(_pruefen(_test_datenbank), "flashboot")
    assert (einmal["ist"], einmal["bewertung"]) == (True, "gemeldet")

    runpod.endpoint = _guter_endpoint(flashboot=False)
    aus = _befund(_pruefen(_test_datenbank), "flashboot")
    assert (aus["ist"], aus["bewertung"]) == (False, "gemeldet")

    runpod.endpoint = _guter_endpoint()
    unbekannt = _befund(_pruefen(_test_datenbank), "flashboot")
    assert unbekannt["bewertung"] == "nicht_pruefbar"


def test_sollwerte_des_endpoints_kommen_aus_der_konfiguration(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`idleTimeout`, `executionTimeoutMs`, `workersMin` und `workersMax` werden
    gegen die eingestellten Werte verglichen, nicht gegen feste Zahlen."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_IDLE_TIMEOUT", "120")
    monkeypatch.setenv("MINERU_OCR_EXECUTION_TIMEOUT_MS", "900000")
    monkeypatch.setenv("MINERU_OCR_WORKERS_MIN", "1")
    monkeypatch.setenv("MINERU_OCR_WORKERS_MAX", "4")

    # Der Endpoint hat noch die alten Werte: vier Abweichungen.
    befund = _pruefen(_test_datenbank)
    assert befund["drift"] == 4
    assert _befund(befund, "idleTimeout")["soll"] == 120
    assert _befund(befund, "workersMax")["soll"] == 4

    runpod.endpoint = _guter_endpoint(
        idleTimeout=120, executionTimeoutMs=900000, workersMin=1, workersMax=4
    )
    assert _pruefen(_test_datenbank)["drift"] == 0


def test_workers_max_wird_auf_den_sollwert_geprueft(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Vorher galt nur „mindestens eins". Jetzt ist es die Kontogrenze, 10."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(workersMax=5)
    maximum = _befund(_pruefen(_test_datenbank), "workersMax")
    assert maximum["soll"] == 10
    assert maximum["bewertung"] == "abweichung"


def test_abweichung_bricht_nur_mit_strict_ab(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Eine echte Abweichung ist mit `strict` ein Abbruch, ohne `strict` eine
    Warnung im Befund."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(idleTimeout=3600)

    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False, strict=True
    ) as klient:
        with pytest.raises(ConfigDriftError):
            klient.preflight()

    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False, strict=False
    ) as nachsichtig:
        befund = nachsichtig.preflight()
    assert befund["drift"] == 1


def test_toter_daemon_wird_erkannt(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Das haeufigste Betriebs-Missverstaendnis: Alles sieht normal aus, nur
    arbeitet niemand. Der Client sagt es."""
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False, strict=False
    ) as klient:
        befund = klient.preflight()

    daemon = [b for b in befund["checks"] if b["punkt"] == "daemon"][0]
    assert daemon["bewertung"] == "warnung"
    assert daemon["ist"] == 0


def test_ocr_start_reiht_auch_ohne_daemon_ein_und_warnt(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Abschicken haengt nicht davon ab, ob gerade ein Daemon laeuft.

    Der Auftraggeber hat erst verlangt, der Client solle ohne Exekutor „sterben
    und errorn", und das spaeter selbst abgeloest: „das warten ist kuenstlich man
    wartet auf die datenbank nicht direkt auf den worker der ist ja unabhaengig".
    Ohne Daemon wird eingereiht, abgebrochen wird nichts, und der Client sagt es
    auf dem Logger statt per Ausnahme.
    """
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        gleich = offener_client.ocr_start(_png("a"))

    assert gleich.queued is True
    assert _jobs_zaehlen(db_conn, gleich) == 1
    assert _zaehlen(db_conn, "inputs") == 1
    assert any("kein Daemon" in eintrag.getMessage() for eintrag in caplog.records)


def test_ocr_start_warnt_auch_bei_altem_herzschlag(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    abgestuerzter_worker: Callable[[], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Eine stehengebliebene Worker-Zeile eines abgestuerzten Daemons gilt nicht
    als laufender Daemon: Dieselbe Warnung, dasselbe Einreihen."""
    abgestuerzter_worker()
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        h = offener_client.ocr_start(_png("a"))

    assert any("kein Daemon" in eintrag.getMessage() for eintrag in caplog.records)
    assert _jobs_zaehlen(db_conn, h) == 1


def test_ocr_start_warnt_je_daemonloser_phase_einmal(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Die Kommandozeile reiht Dokument fuer Dokument ein; bei dreissigtausend
    Dokumenten ohne Daemon waeren dreissigtausend gleiche Zeilen auf stderr
    keine Warnung mehr, sondern Laerm. Gewarnt wird einmal, solange kein Daemon
    laeuft — und erneut, wenn nach einem laufenden wieder keiner da ist."""
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        offener_client.ocr_start(_png("0"))
        offener_client.ocr_start(_png("1"))
        assert _warnungen_des_clients(caplog) == 1

        # Ein Daemon kommt hoch ...
        _worker_am_leben(db_conn)
        offener_client.ocr_start(_png("2"))
        assert _warnungen_des_clients(caplog) == 1

        # ... und faellt wieder aus: Das ist eine neue Lage, und sie wird gesagt.
        db_conn.execute("DELETE FROM procrastinate_workers")
        offener_client.ocr_start(_png("3"))
        assert _warnungen_des_clients(caplog) == 2


@pytest.mark.parametrize("beobachter", ["status", "preflight"])
def test_ocr_start_warnt_erneut_auch_wenn_der_daemon_nur_anderswo_gesehen_wurde(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    beobachter: str,
) -> None:
    """Ein langlebiger Client sieht den Daemon nicht nur beim Abschicken: `status`
    und `preflight` fragen dieselbe Lebendigkeit. Kommt ein Daemon hoch, wird nur
    dort gesehen und faellt aus, ist das fuer das naechste Abschicken eine NEUE
    daemonlose Phase — und sie wird gesagt."""
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        offener_client.ocr_start(_png("0"))
        assert _warnungen_des_clients(caplog) == 1

        _worker_am_leben(db_conn)
        getattr(offener_client, beobachter)()  # sieht den Daemon

        db_conn.execute("DELETE FROM procrastinate_workers")
        offener_client.ocr_start(_png("1"))
        assert _warnungen_des_clients(caplog) == 2


async def test_ocr_start_warnt_nicht_wenn_nichts_eingereiht_wird(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Die Warnung sagt „die Jobs sind eingereiht“. Wird nichts eingereiht —
    eine leere Folge, lauter fertige Dokumente —, waere sie falsch."""
    _worker_am_leben(db_conn)
    fertig = _png("fertig")
    h = offener_client.ocr_start(fertig)
    await write_terminal_async(async_conn, sha256=h, status=DocumentStatus.DONE, raw={})
    db_conn.execute("DELETE FROM procrastinate_workers")

    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        assert offener_client.ocr_start([]) == []
        assert offener_client.ocr_start(fertig).queued is False

    assert _warnungen_des_clients(caplog) == 0


def test_ocr_start_reiht_auch_ein_wenn_die_lebendigkeit_nicht_lesbar_ist(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Die Lebendigkeit ist nur Auskunft. Fehlt Procrastinates undokumentierte
    Worker-Tabelle (oder sieht sie nach einem Upgrade anders aus), scheitert
    `db.worker_heartbeats` — und das Abschicken darf daran nicht haengen."""

    _lebendigkeit_unlesbar(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        h = offener_client.ocr_start(_png("a"))

    assert h.queued is True
    assert _jobs_zaehlen(db_conn, h) == 1
    meldungen = [e.getMessage() for e in caplog.records]
    assert any("nicht pruefbar" in m for m in meldungen)
    # Die Ursache steht im Log: Eine weggebrochene Verbindung ist etwas anderes
    # als eine umgebaute Worker-Tabelle, und der Betreiber soll nicht an der
    # falschen Stelle suchen.
    assert any("verbindung weg" in m for m in meldungen)


def test_ocr_start_warnt_erneut_wenn_der_grund_wechselt(
    offener_client: OCRClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erst ist die Lebendigkeit nicht lesbar, dann lesbar, aber es laeuft kein
    Daemon: Das sind zwei verschiedene Lagen, und die zweite wird auch gesagt,
    obwohl zwischendurch nie ein Daemon zu sehen war."""
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        with monkeypatch.context() as kaputt:
            _lebendigkeit_unlesbar(kaputt)
            offener_client.ocr_start(_png("0"))
            offener_client.ocr_start(_png("1"))  # derselbe Grund: still
        assert _warnungen_des_clients(caplog) == 1

        offener_client.ocr_start(_png("2"))  # jetzt lesbar, aber kein Daemon

    meldungen = [e.getMessage() for e in caplog.records]
    assert len(meldungen) == 2
    assert "nicht pruefbar" in meldungen[0]
    assert "kein Daemon" in meldungen[1]


def test_status_meldet_den_daemon_als_unbekannt_wenn_die_lebendigkeit_nicht_lesbar_ist(
    offener_client: OCRClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Lebendigkeit ist nur Auskunft: Eine unlesbare Worker-Tabelle laesst
    `status` nicht scheitern, sie macht nur diese eine Angabe unbekannt — wie das
    Guthaben, wenn RunPod nicht antwortet."""
    _lebendigkeit_unlesbar(monkeypatch)

    uebersicht = offener_client.status()

    assert uebersicht["daemon_alive"] is None
    assert "documents" in uebersicht


def test_ocr_start_warnt_ohne_daemon_nur_einmal_je_client(
    offener_client: OCRClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Die Kommandozeile ruft `ocr_start` je Datei; dieselbe Warnung dreissigtausend
    Mal liesse die eigentlichen Meldungen darin untergehen. Der Client sagt es
    einmal."""
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        offener_client.ocr_start(_png("a"))
        offener_client.ocr_start(_png("b"))
        offener_client.ocr_start([_png("c"), _png("d")])

    assert caplog.text.count("kein Daemon") == 1


def test_ocr_start_wenn_ein_anderer_schneller_war_legt_nichts_an(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zwischen dem Nachsehen und dem Anlegen kann ein anderer Aufrufer dasselbe
    Dokument anlegen. Dann ist es unterwegs, und dieser Aufruf fasst weder die
    Eingabe noch einen Job an."""
    echtes_anlegen = db.insert_document

    def anderer_war_schneller(_conn: Any, **angaben: Any) -> bool:
        echtes_anlegen(db_conn, **angaben)  # der andere, auf seiner Verbindung
        return False

    monkeypatch.setattr(db, "insert_document", anderer_war_schneller)

    gereicht = offener_client.ocr_start(_png("rennen"))

    assert gereicht.queued is False
    assert gereicht.status is DocumentStatus.NEW
    assert _zaehlen(db_conn, "documents") == 1
    assert _zaehlen(db_conn, "inputs") == 0
    assert _zaehlen(db_conn, "procrastinate_jobs") == 0


def test_ocr_start_wenn_der_job_schon_besteht_rollt_alles_zurueck(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Besteht zum Hash schon ein Job (`queueing_lock`), wird Zeile und Eingabe
    dieses Aufrufs nicht stehen gelassen: sonst gaebe es eine Zeile `new`, die
    niemand je anfasst."""
    monkeypatch.setattr(tasks, "enqueue_document", lambda *_a, **_k: False)

    gereicht = offener_client.ocr_start(_png("schon da"))

    assert gereicht.queued is False
    assert gereicht.status is DocumentStatus.NEW
    assert _zaehlen(db_conn, "documents") == 0
    assert _zaehlen(db_conn, "inputs") == 0


def test_ocr_start_warnt_nicht_bei_laufendem_daemon(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _worker_am_leben(db_conn)
    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        offener_client.ocr_start(_png("a"))

    assert not [e for e in caplog.records if e.name == "mineru_ocr.client"]


def test_preflight_meldet_den_daemon_nach_dem_alter_des_herzschlags(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    abgestuerzter_worker: Callable[[], None],
    runpod: RunPodAntworten,
) -> None:
    """Ein 67 Tage alter Herzschlag ist ein toter Daemon, ein frischer ein
    lebender — im selben Befund, am selben Punkt."""
    abgestuerzter_worker()
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False, strict=False
    ) as klient:
        tot = [b for b in klient.preflight()["checks"] if b["punkt"] == "daemon"][0]
    assert tot["bewertung"] == "warnung"
    assert tot["ist"] == 0

    _worker_am_leben(db_conn)
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False, strict=False
    ) as klient:
        lebt = [b for b in klient.preflight()["checks"] if b["punkt"] == "daemon"][0]
    assert lebt["bewertung"] == "in_ordnung"
    assert lebt["ist"] == 1


def test_status_meldet_den_daemon_nach_dem_alter_des_herzschlags(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    abgestuerzter_worker: Callable[[], None],
) -> None:
    abgestuerzter_worker()
    assert offener_client.status()["daemon_alive"] is False

    _worker_am_leben(db_conn)
    assert offener_client.status()["daemon_alive"] is True


# --- Arbeit ---------------------------------------------------------------


def test_ocr_start_nimmt_bytes_und_gibt_den_hash(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Es reiht ein und kehrt zurueck — „das losschicken ist einfach einen job
    erstellen". Zurueck kommt der Hash, der Griff, mit dem es in jedem Prozess
    weitergeht."""
    _worker_am_leben(db_conn)
    inhalt = _png("a")
    h = offener_client.ocr_start(inhalt)

    assert h == _hash(inhalt)
    assert isinstance(h, str)
    anzahl = db_conn.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE queueing_lock = %s", (h,)
    ).fetchone()
    assert anzahl is not None and anzahl[0] == 1
    assert h.queued is True
    assert h.status is DocumentStatus.NEW


def test_ocr_start_viele_gibt_je_dokument_den_hash_in_eingabereihenfolge(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    _worker_am_leben(db_conn)
    inhalte = [_png("c"), _png("a"), _png("b")]
    hashes = offener_client.ocr_start(inhalte)

    assert hashes == [_hash(i) for i in inhalte]
    assert _zaehlen(db_conn, "procrastinate_jobs") == 3
    assert _zaehlen(db_conn, "inputs") == 3


def test_ocr_start_prueft_erst_alle_und_reiht_dann_ein(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Bei einem abgewiesenen Element waeren die vorderen sonst schon abgeschickt,
    und der Aufrufer bekaeme eine Ausnahme statt ihrer Hashes."""
    _worker_am_leben(db_conn)
    with pytest.raises(UnsupportedTypeError):
        offener_client.ocr_start([_png("gut"), b"nur Text, kein Dokument"])

    for tabelle in ("documents", "inputs", "procrastinate_jobs"):
        assert _zaehlen(db_conn, tabelle) == 0, tabelle


def test_ocr_start_weist_ein_bild_ueber_der_groessengrenze_ab_ohne_etwas_einzureihen(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Ein Bild (wie jedes Ganzdokument-Format) laesst sich nicht zerlegen; ueber
    der Groessengrenze einer Anfrage kann daraus nichts werden. Das weist schon das
    Abschicken ab, und das vordere, gute Dokument bleibt ebenfalls draussen."""
    _worker_am_leben(db_conn)
    zu_gross = PNG_KOPF + b"\x00" * offener_client.settings.max_payload_bytes
    with pytest.raises(TooLargeError, match="AUFRUESTBAR"):
        offener_client.ocr_start([_png("gut"), zu_gross])

    for tabelle in ("documents", "inputs", "procrastinate_jobs"):
        assert _zaehlen(db_conn, tabelle) == 0, tabelle


def test_ocr_start_weist_eine_eingabe_ueber_der_ablagegrenze_ab(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auch eine PDF: Ueber `db.MAX_INPUT_BYTES` laesst sich nichts in `inputs`
    ablegen (Postgres begrenzt ein Feld auf knapp 1 GiB)."""
    from mineru_ocr import db

    monkeypatch.setattr(db, "MAX_INPUT_BYTES", 50)
    pdf = b"%PDF-1.7\n" + b"x" * 100
    with pytest.raises(TooLargeError, match="Datenbank"):
        offener_client.ocr_start(pdf)
    assert _zaehlen(db_conn, "documents") == 0


def test_ocr_start_prueft_eine_pdf_nicht_gegen_die_groessengrenze_einer_anfrage(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Eine einzelne PDF-Seite ueber der Grenze findet erst das Schneiden im Job;
    das Abschicken kennt die Seiten nicht und laesst die PDF durch."""
    _worker_am_leben(db_conn)
    pdf = b"%PDF-1.7\n" + b"x" * offener_client.settings.max_payload_bytes
    h = offener_client.ocr_start(pdf)
    assert h.queued is True


async def test_ocr_start_faesst_ein_ergebnis_nicht_an(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """Ein Treffer legt nichts an und reiht nichts ein — sonst waere jedes erneute
    Durchlaufen eines Ordners ein vollstaendig neu bezahlter Bestand."""
    _worker_am_leben(db_conn)
    fertig = _png("fertig")
    h_fertig = offener_client.ocr_start(fertig)
    await write_terminal_async(
        async_conn, sha256=h_fertig, status=DocumentStatus.DONE, raw={"markdown": "x"}
    )
    db_conn.execute("DELETE FROM procrastinate_jobs")
    vorher = db_conn.execute(
        "SELECT updated_at FROM documents WHERE sha256 = %s", (h_fertig,)
    ).fetchone()

    wieder = offener_client.ocr_start(fertig)

    assert wieder == h_fertig
    assert wieder.queued is False
    assert wieder.status is DocumentStatus.DONE
    assert _zaehlen(db_conn, "procrastinate_jobs") == 0
    assert _zaehlen(db_conn, "inputs") == 0, "die Eingabe darf nicht zurueckkehren"
    nachher = db_conn.execute(
        "SELECT updated_at FROM documents WHERE sha256 = %s", (h_fertig,)
    ).fetchone()
    assert nachher == vorher


async def test_ocr_start_faesst_auch_ein_gescheitertes_dokument_nicht_an(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """Auch bei `failed` ist das ein Treffer: Ein gescheitertes Dokument liest nur
    `retry` neu."""
    _worker_am_leben(db_conn)
    inhalt = _png("gescheitert")
    h = offener_client.ocr_start(inhalt)
    await write_terminal_async(
        async_conn, sha256=h, status=DocumentStatus.FAILED, raw={}, error="aus"
    )
    db_conn.execute("DELETE FROM procrastinate_jobs")

    wieder = offener_client.ocr_start(inhalt)

    assert wieder.queued is False
    assert wieder.status is DocumentStatus.FAILED
    assert _zaehlen(db_conn, "procrastinate_jobs") == 0
    assert _zaehlen(db_conn, "inputs") == 1, "bei `failed` bleibt die Eingabe"


def test_ocr_start_reiht_ein_unterwegs_liegendes_dokument_nicht_nochmal_ein(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Eine Zeile `new` heisst unterwegs. Das gilt auch, wenn ihr Job schon
    endgueltig endete, ohne etwas zu schreiben: Das ist der Fall fuer `retry`, nicht
    fuer erneutes Abschicken."""
    _worker_am_leben(db_conn)
    inhalt = _png("unterwegs")
    offener_client.ocr_start(inhalt)
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")

    wieder = offener_client.ocr_start(inhalt)

    assert wieder.queued is False
    assert wieder.status is DocumentStatus.NEW
    assert _zaehlen(db_conn, "procrastinate_jobs") == 1


def test_ocr_start_mit_demselben_dokument_mehrfach_reiht_es_einmal_ein(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    _worker_am_leben(db_conn)
    inhalt = _png("doppelt")
    erste, zweite = offener_client.ocr_start([inhalt, inhalt])

    assert erste == zweite
    assert (erste.queued, zweite.queued) == (True, False)
    assert _zaehlen(db_conn, "procrastinate_jobs") == 1


def test_derselbe_inhalt_in_zwei_aufrufen_ist_eine_zeile_und_eine_eingabe(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Der rohe Byte-Hash ist die Identitaet. Wo die Bytes herkamen, merkt sich
    nichts: Die Datenbank kennt keine Pfade."""
    inhalt = _png("gleicher inhalt")
    erste = offener_client.ocr_start(inhalt)
    zweite = offener_client.ocr_start(inhalt)

    assert erste == zweite
    assert _zaehlen(db_conn, "documents") == 1
    assert _zaehlen(db_conn, "inputs") == 1
    zeile = db_conn.execute(
        "SELECT content FROM inputs WHERE sha256 = %s", (erste,)
    ).fetchone()
    assert zeile is not None and bytes(zeile[0]) == inhalt


def test_kein_metadaten_normalisierter_hash(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Zwei PDFs mit gleichem Seiteninhalt, aber verschiedenen Bytes sind ZWEI
    Dokumente — es wird nichts wegnormalisiert.

    Ein Normalisierungsfehler waere der unsichtbarste Fehler ueberhaupt: „gilt als
    fertig, wurde nie erkannt". Deshalb ist der Hash der ROHE Byte-Hash.
    """
    ohne = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
    mit = b"%PDF-1.7\n%Zusatz\n1 0 obj\n<<>>\nendobj\n"
    a, b = offener_client.ocr_start([ohne, mit])

    assert a != b
    assert _zaehlen(db_conn, "documents") == 2


def test_abschicken_oeffnet_keine_pdf(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Eine kaputte PDF bricht das Abschicken nicht ab: Sie wird abgelegt, ohne
    Seitenzahl, und endet im Job als `input_error`. Der Beweis, dass nichts
    geoeffnet wurde: Bytes, die nach PDF aussehen und sich nicht oeffnen lassen,
    kommen durch."""
    _worker_am_leben(db_conn)
    kaputt = b"%PDF-1.7\nund dann nur noch Unsinn\n"
    h = offener_client.ocr_start(kaputt)

    huelle = offener_client.get(h)
    assert huelle.status is DocumentStatus.NEW
    assert huelle.page_count is None


def test_ocr_start_weist_pfade_und_text_ab_statt_sie_zu_lesen(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Die Bibliothek kennt keine Pfade. Wer einen uebergibt, bekommt eine klare
    Auskunft statt eines Absturzes tief im Typ-Erkennen — und es ist nichts
    eingereiht."""
    with pytest.raises(TypeError, match="Bytes"):
        offener_client.ocr_start("scan.pdf")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Bytes"):
        offener_client.ocr_start([_png("gut"), "scan.pdf"])  # type: ignore[list-item]
    assert _zaehlen(db_conn, "documents") == 0


@pytest.mark.parametrize(
    "falsch", [Path("scan.pdf"), None, 5, bytearray(b"x"), memoryview(b"x")]
)
def test_ocr_start_nennt_bei_jedem_falschen_typ_dieselbe_klare_auskunft(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any], falsch: Any
) -> None:
    """Auch ein Pfad, `None`, eine Zahl oder ein veraenderlicher Puffer gibt die
    Auskunft „nimmt Bytes", nicht ein „nicht iterierbar" oder eine Liste von
    Zahlen."""
    with pytest.raises(TypeError, match="ocr_start nimmt Bytes"):
        offener_client.ocr_start(falsch)
    assert _zaehlen(db_conn, "documents") == 0


def test_get_document_und_wait_nehmen_einen_hash_und_keinen_pfad(
    offener_client: OCRClient,
) -> None:
    with pytest.raises(TypeError, match="sha256"):
        offener_client.get(Path("scan.pdf"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="sha256"):
        offener_client.document(Path("scan.pdf"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="sha256"):
        offener_client.wait(Path("scan.pdf"), timeout=0.1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fehler",
    [DatabaseError("Die Worker-Lebenszeichen liessen sich nicht lesen"), psycopg.OperationalError("weg")],
)
def test_ocr_start_gibt_den_hash_auch_wenn_die_abfrage_der_lebenszeichen_scheitert(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fehler: Exception,
) -> None:
    """Die Warnung ist eine Auskunft. Das Dokument ist da schon eingereiht; scheitert
    dann die Abfrage, bekommt der Aufrufer trotzdem seinen Hash und keine
    Ausnahme. `db.worker_heartbeats` wirft dabei den uebersetzten `DatabaseError`,
    nicht psycopgs eigenen Fehler."""

    def scheitert(_conn: Any) -> Any:
        raise fehler

    monkeypatch.setattr(db, "worker_heartbeats", scheitert)

    with caplog.at_level(logging.WARNING, logger="mineru_ocr.client"):
        gereicht = offener_client.ocr_start(_png("trotzdem"))

    assert gereicht.queued is True
    assert _zaehlen(db_conn, "procrastinate_jobs") == 1
    assert "nicht pruefbar" in caplog.text


def test_die_bibliothek_kennt_keine_pfade() -> None:
    """Kein Parameter der Bibliothek ist ein Pfad, und die entfallenen Namen gibt
    es nicht mehr — auch nicht in den Exporten des Pakets.

    Pfade kennt nur die Kommandozeile (`mineru_ocr.cli`); sie ist deshalb die eine
    Datei, die hier nicht geprueft wird.
    """
    import inspect

    import mineru_ocr
    from mineru_ocr import core, db, mime, wrapper

    for modul in (client_modul, core, wrapper, mime, db):
        for name, objekt in inspect.getmembers(modul):
            if name.startswith("_") or getattr(objekt, "__module__", None) != modul.__name__:
                continue
            kandidaten = [objekt]
            if inspect.isclass(objekt):
                kandidaten = [
                    m
                    for n, m in inspect.getmembers(objekt, inspect.isroutine)
                    if not n.startswith("_") or n == "__init__"
                ]
            for funktion in kandidaten:
                if not callable(funktion):
                    continue
                try:
                    parameter = inspect.signature(funktion).parameters
                except (TypeError, ValueError):
                    continue
                for pname, param in parameter.items():
                    assert "Path" not in str(param.annotation), (
                        modul.__name__,
                        name,
                        pname,
                    )

    for entfallen in ("ocr_path", "check_daemon"):
        assert not hasattr(OCRClient, entfallen), entfallen
    for entfallen in ("from_path", "from_bytes"):
        assert not hasattr(OCRWrapper, entfallen), entfallen
    assert not hasattr(wrapper, "OCRMegaWrapper")
    assert not hasattr(mineru_ocr, "OCRMegaWrapper")
    assert "OCRMegaWrapper" not in mineru_ocr.__all__


def test_der_griff_ueberlebt_kopieren_und_pickeln_samt_seiner_angaben() -> None:
    """`Submitted` ist ein `str` mit zwei Angaben. Ein Griff, der beim Kopieren
    abstuerzt oder seine Angaben verliert, waere eine Falle fuer jeden, der Hashes
    ueber Prozessgrenzen reicht (`multiprocessing`, `copy.deepcopy`)."""
    import copy
    import json
    import pickle

    griff = core.Submitted("ab" * 32, queued=True, status=DocumentStatus.NEW)

    for kopie in (copy.copy(griff), copy.deepcopy(griff), pickle.loads(pickle.dumps(griff))):
        assert kopie == "ab" * 32
        assert isinstance(kopie, core.Submitted)
        assert kopie.queued is True
        assert kopie.status is DocumentStatus.NEW
    assert json.dumps({"h": griff}) == json.dumps({"h": "ab" * 32})
    als_schluessel: dict[str, int] = {griff: 1}
    assert als_schluessel["ab" * 32] == 1


def test_zeile_eingabe_und_job_entstehen_zusammen_oder_gar_nicht(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheitert das Einreihen, bleibt weder die Zeile noch die Eingabe stehen:
    eine Zeile `new` ohne Job heisst „unterwegs", und niemand wuerde sie je wieder
    anfassen."""
    _worker_am_leben(db_conn)

    def scheitert(*_: Any, **__: Any) -> bool:
        raise psycopg.OperationalError("Verbindung weg")

    monkeypatch.setattr(tasks, "enqueue_document", scheitert)
    with pytest.raises(psycopg.OperationalError):
        offener_client.ocr_start(_png("atomar"))

    for tabelle in ("documents", "inputs", "procrastinate_jobs"):
        assert _zaehlen(db_conn, tabelle) == 0, tabelle


def test_ocr_start_laedt_kein_raw(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heute laedt das Registrieren eines fertigen Dokuments sein ganzes `raw` mit;
    ein erneuter Lauf ueber 30.000 fertige Dokumente zoege so rund 34 GB."""
    from mineru_ocr import db

    inhalt = _png("fertig")
    h = _hash(inhalt)
    db_conn.execute(
        "INSERT INTO documents (sha256, size_bytes, mime, endpoint_id, status, raw) "
        "VALUES (%s, 1, 'image/png', 'e', 'done', %s::jsonb)",
        (h, '{"markdown": "gross"}'),
    )
    gelesen: list[bool] = []
    echtes_lesen = db.get_document

    def merkt(conn: Any, sha256: str, *, with_raw: bool = True) -> Any:
        gelesen.append(with_raw)
        return echtes_lesen(conn, sha256, with_raw=with_raw)

    monkeypatch.setattr(db, "get_document", merkt)
    offener_client.ocr_start(inhalt)

    assert gelesen and not any(gelesen), "das Abschicken hat raw gelesen"


async def test_wait_gibt_bei_jedem_endzustand_zurueck(
    offener_client: OCRClient, async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auch bei `failed` — das Scheitern ist ein Ergebnis, kein Fehler des
    Wartens. Der Aufrufer prueft `.success`."""
    h = _abschicken(offener_client, "gescheitert")
    await write_terminal_async(
        async_conn,
        sha256=h,
        status=DocumentStatus.FAILED,
        raw={"markdown": "Teil"},
        error="aus",
    )

    zurueck = offener_client.wait(h, timeout=5.0)
    assert zurueck.status is DocumentStatus.FAILED
    assert zurueck.success is False


def test_wait_auf_unbekannten_hash_wirft_sofort(offener_client: OCRClient) -> None:
    """Nicht erst nach Ablauf der Wartezeit. Auf etwas zu warten, das nie
    eingereiht wurde, ist immer ein Irrtum des Aufrufers."""
    with pytest.raises(DocumentNotFound):
        offener_client.wait("f" * 64, timeout=30.0)


def test_ocr_liefert_das_ergebnis_eines_treffers_ohne_etwas_einzureihen(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """`ocr(data)` ist Abschicken plus Warten auf dieses eine Dokument. Hier ist das
    Ergebnis schon da (ein Treffer): Das Abschicken tut nichts, das Warten kehrt
    sofort zurueck."""
    inhalt = _png("schon da")
    db_conn.execute(
        "INSERT INTO documents (sha256, size_bytes, mime, endpoint_id, status, raw) "
        "VALUES (%s, 1, 'image/png', 'e', 'done', %s::jsonb)",
        (
            _hash(inhalt),
            '{"markdown": "steht da", "content_list": [], "images": {}}',
        ),
    )

    ergebnis = offener_client.ocr(inhalt, timeout=5.0)

    assert isinstance(ergebnis, OCRWrapper)
    assert ergebnis["markdown"] == "steht da"
    assert _zaehlen(db_conn, "procrastinate_jobs") == 0


def test_ocr_wartet_auf_das_dokument_und_laeuft_in_die_frist(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Ohne Daemon wird nichts fertig: Die Frist beendet das Warten, nicht das
    Dokument."""
    _worker_am_leben(db_conn)
    with pytest.raises(TimeoutError):
        offener_client.ocr(_png("wartet"), timeout=0.3)
    # Das Dokument ist danach weiter unterwegs.
    assert _zaehlen(db_conn, "procrastinate_jobs") == 1


def test_get_und_document_auf_unbekannten_hash_werfen(
    offener_client: OCRClient,
) -> None:
    with pytest.raises(DocumentNotFound):
        offener_client.get("f" * 64)
    with pytest.raises(DocumentNotFound):
        offener_client.document("f" * 64)


async def test_get_holt_das_ergebnis_ohne_zu_warten_und_laedt_raw_erst_bei_bedarf(
    offener_client: OCRClient, async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """`get(h)` gibt die Huelle zu einem bekannten Dokument. Die Huelle des
    Sync-Clients laedt `raw` bei Bedarf ueber die Verbindung des Clients nach."""
    h = _abschicken(offener_client, "abholen")
    await write_terminal_async(
        async_conn,
        sha256=h,
        status=DocumentStatus.DONE,
        raw={"markdown": "steht in der Datenbank", "content_list": [], "images": {}},
    )

    huelle = offener_client.get(h)

    assert huelle.row.raw_loaded is False
    assert huelle["markdown"] == "steht in der Datenbank"
    assert huelle.row.raw_loaded is True


def test_get_gibt_auch_ein_offenes_dokument(offener_client: OCRClient) -> None:
    """Ob das Dokument fertig ist, sagt die Huelle (`status`), nicht `get`."""
    h = _abschicken(offener_client, "offen")
    assert offener_client.get(h).status is DocumentStatus.NEW


async def test_retry_failed_nimmt_nur_failed(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """`input_error` wird uebersprungen und nicht mitgezaehlt: Ein so
    wiedervorgelegter Job taete nichts, verbrauchte aber einen Versuch.

    Der Fall ist konkret: Ein zu grosses DOCX liesse sich damit scheinbar
    wiederholen, bliebe `input_error`, und beim naechsten echten Bedarf waere das
    Budget aufgebraucht.
    """
    _worker_am_leben(db_conn)
    gescheitert = _abschicken(offener_client, "failed")
    eingabefehler = _abschicken(offener_client, "input_error")
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")

    await write_terminal_async(
        async_conn, sha256=gescheitert, status=DocumentStatus.FAILED, raw={}
    )
    await write_terminal_async(
        async_conn, sha256=eingabefehler, status=DocumentStatus.INPUT_ERROR, raw={}
    )

    assert offener_client.retry_failed(offener_client.get(eingabefehler)) == 0
    assert offener_client.retry_failed(offener_client.get(gescheitert)) == 1


async def test_retry_failed_ueberspringt_aborted(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """Das Tooling weist `aborted` ab. Uebersprungen und gemeldet, nicht mit einer
    Ausnahme aufgehalten."""
    _worker_am_leben(db_conn)
    h = _abschicken(offener_client, "abgebrochen")
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'aborted'")
    await write_terminal_async(async_conn, sha256=h, status=DocumentStatus.FAILED, raw={})

    # Kein Absturz, sondern eine ehrliche Null.
    assert offener_client.retry_failed(offener_client.get(h)) == 0


# --- Nachsehen ------------------------------------------------------------


async def test_status_zaehlt_in_summen(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """Zwei Abfragen, unabhaengig von der Bestandsgroesse — nicht eine je
    Dokument.

    Bei zehntausend Dokumenten waere die Einzelaufloesung zehntausend Abfragen
    fuer eine Uebersicht, die niemand Dokument fuer Dokument liest.
    """
    _worker_am_leben(db_conn)
    hashes = [_abschicken(offener_client, str(nr)) for nr in range(3)]
    await write_terminal_async(
        async_conn, sha256=hashes[0], status=DocumentStatus.DONE, raw={}
    )

    uebersicht = offener_client.status()
    assert uebersicht["documents"]["done"] == 1
    assert uebersicht["documents"]["new"] == 2
    assert uebersicht["jobs"]["todo"] == 3
    assert uebersicht["daemon_alive"] is True
    assert uebersicht["balance"] == 5.0
    # Jeder Dokument-Zustand kommt vor, auch mit Null.
    assert set(uebersicht["documents"]) == {
        z.value for z in DocumentStatus
    }


def test_credit_wird_live_geholt(
    offener_client: OCRClient, runpod: RunPodAntworten
) -> None:
    """Kein zwischengespeicherter Wert: Ein alter Kontostand ist genau dann
    falsch, wenn es darauf ankommt."""
    assert offener_client.credit() == 5.0
    runpod.kontostand = 0.25
    assert offener_client.credit() == 0.25


def test_workers_liefert_einzeln(
    offener_client: OCRClient, runpod: RunPodAntworten
) -> None:
    """Nicht die zusammengefassten Zaehler des Endpoints — die luegen."""
    runpod.worker = [{"id": "a", "desiredStatus": "RUNNING"}, {"id": "b"}]
    assert [w["id"] for w in offener_client.workers()] == ["a", "b"]


def test_settings_maskiert_den_schluessel(offener_client: OCRClient) -> None:
    """Auch ueber `client.settings`.

    Eine Einstellung landet frueher oder spaeter in einem Log, einer
    Fehlermeldung oder einer Debugger-Ausgabe. Der Schluessel erscheint dort NIE
    im Klartext.
    """
    assert "erfunden" not in repr(offener_client.settings)
    assert "***" in repr(offener_client.settings)
    # Der Wert selbst ist natuerlich noch da — wer ihn braucht, liest ihn
    # bewusst aus dem Feld.
    assert offener_client.settings.api_key == "erfunden"


def test_conn_vor_dem_betreten_wirft(_test_datenbank: str) -> None:
    """Ein Client, der nicht betreten wurde, hat keine Verbindung — und sagt es,
    statt eine halb aufgebaute herauszugeben."""
    klient = OCRClient(db_dsn=_test_datenbank, api_key="x", preflight=False)
    with pytest.raises(ConfigurationError, match="betreten"):
        klient.conn


def test_procrastinate_app_vor_dem_betreten_und_nach_dem_verlassen_wirft(
    _test_datenbank: str, runpod: RunPodAntworten
) -> None:
    """Die App des Clients gibt es nur zwischen Betreten und Verlassen: Davor und
    danach sagt der Client es, statt eine fehlende oder geschlossene App
    herauszugeben."""
    klient = OCRClient(db_dsn=_test_datenbank, api_key="x", preflight=False)
    with pytest.raises(ConfigurationError, match="betreten"):
        klient._procrastinate
    with klient:
        assert klient._procrastinate is not None
    with pytest.raises(ConfigurationError, match="betreten"):
        klient._procrastinate


def _pools_aufzeichnen(
    monkeypatch: pytest.MonkeyPatch,
) -> list[NullConnectionPool[Any]]:
    """Ersetzt den Pool-Typ des Clients durch einen, der seine Pools merkt."""
    angelegt: list[NullConnectionPool[Any]] = []

    class Aufzeichnend(NullConnectionPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            angelegt.append(self)

    monkeypatch.setattr(client_modul, "NullConnectionPool", Aufzeichnend)
    return angelegt


class _Absichtlich(Exception):
    """Der Fehler, den die Tests unterwegs ausloesen."""


def test_scheitert_der_preflight_beim_betreten_wird_alles_wieder_zugemacht(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ein `with` ruft `__exit__` nicht, wenn schon `__enter__` wirft. Scheitert
    also der Preflight, nachdem Verbindung, Pool und App offen sind, muss der
    Client sie selbst wieder zumachen — und den urspruenglichen Fehler weitergeben.
    """
    pools = _pools_aufzeichnen(monkeypatch)

    def kaputt(self: OCRClient) -> dict[str, Any]:
        raise _Absichtlich("preflight")

    monkeypatch.setattr(OCRClient, "preflight", kaputt)
    vorher = _verbindungen_stabil(db_conn)
    klient = OCRClient(db_dsn=_test_datenbank, api_key="x", preflight=True)

    with pytest.raises(_Absichtlich, match="preflight"):
        klient.__enter__()

    assert len(pools) == 1 and pools[0].closed
    assert klient._conn is None and klient._pool is None and klient._app is None
    assert _verbindungen_erreichen(db_conn, vorher), "Verbindung blieb offen"


def test_scheitert_das_bauen_der_app_beim_betreten_wird_alles_wieder_zugemacht(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dasselbe, wenn es schon beim Bauen der App scheitert: Verbindung und Pool
    sind dann offen, die App nicht."""
    pools = _pools_aufzeichnen(monkeypatch)

    def kaputt(settings: Settings) -> Any:
        raise _Absichtlich("app")

    monkeypatch.setattr(tasks, "build_client_app", kaputt)
    vorher = _verbindungen_stabil(db_conn)
    klient = OCRClient(db_dsn=_test_datenbank, api_key="x", preflight=False)

    with pytest.raises(_Absichtlich, match="app"):
        klient.__enter__()

    assert len(pools) == 1 and pools[0].closed
    assert klient._conn is None and klient._pool is None and klient._app is None
    assert _verbindungen_erreichen(db_conn, vorher), "Verbindung blieb offen"


# --- Die Procrastinate-Anbindung des Clients -------------------------------


def _verbindungen(conn: psycopg.Connection[Any]) -> int:
    """Wie viele andere Verbindungen gerade an der Test-Datenbank haengen."""
    zeile = conn.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchone()
    assert zeile is not None
    return int(zeile[0])


def _verbindungen_erreichen(
    conn: psycopg.Connection[Any], erwartet: int, *, sekunden: float = 3.0
) -> bool:
    """Wartet, bis die Zahl der Verbindungen `erwartet` ist.

    Warten, weil Postgres das Ende einer Verbindung einen Augenblick nach dem
    Schliessen auf der Client-Seite zeigt. Wer eine stehende Verbindung hat, wartet
    die ganze Zeit vergeblich und bekommt False.
    """
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if _verbindungen(conn) == erwartet:
            return True
        time.sleep(0.02)
    return _verbindungen(conn) == erwartet


def _verbindungen_stabil(conn: psycopg.Connection[Any], *, sekunden: float = 3.0) -> int:
    """Die Zahl der Verbindungen, sobald zwei Lesungen im Abstand gleich sind.

    Eine einzelne Lesung kann eine Verbindung des Vortests noch mitzaehlen, die
    Postgres erst einen Augenblick spaeter als beendet zeigt; die Ausgangszahl
    waere dann zu hoch, und kein Warten auf eine feste Zahl holte das ein.
    """
    letzte = _verbindungen(conn)
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        time.sleep(0.1)
        jetzt = _verbindungen(conn)
        if jetzt == letzte:
            return jetzt
        letzte = jetzt
    return letzte


def test_client_haelt_zwischen_aufrufen_keine_procrastinate_verbindung(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
) -> None:
    """Ein Client haelt EINE Verbindung — seine eigene —, keine fuer Procrastinate.

    Je Agent eine Verbindung statt fuenf: Der Server reicht sonst neben dem Daemon
    fuer hoechstens 14 Agenten. Gezaehlt wird von AUSSEN, an der Datenbank, und
    nicht am Pool des Clients: Ein Pool mit stehenden Verbindungen faellt so auf,
    auch wenn jemand ihn anders baut.

    Die Ausgangszahl wird VOR dem Betreten genommen. Mit dem Betreten zaehlte ein
    Pool mit stehenden Verbindungen schon in die Ausgangszahl, und der Test
    bestuende zu Unrecht.
    """
    _worker_am_leben(db_conn)
    vorher = _verbindungen_stabil(db_conn)

    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False
    ) as klient:
        assert _verbindungen_erreichen(db_conn, vorher + 1), "nach dem Betreten"

        h = _abschicken(klient, "a.png")
        klient.document(h)
        klient.status()
        assert _verbindungen_erreichen(db_conn, vorher + 1), "zwischen den Aufrufen"

    assert _verbindungen_erreichen(db_conn, vorher), "nach dem Verlassen"


def test_client_oeffnet_einen_pool_ohne_stehende_verbindungen(
    offener_client: OCRClient,
) -> None:
    """Der Pool des Clients ist ein `NullConnectionPool` mit `max_size=1` und einem
    einzigen Hintergrund-Faden — er legt keine Verbindung auf Vorrat an, und eine
    Abfrage nach der anderen reicht."""
    pool = offener_client._pool
    assert isinstance(pool, NullConnectionPool)
    assert pool.min_size == 0
    assert pool.max_size == 1
    assert pool.num_workers == 1
    assert pool.closed is False


def test_einreihen_holt_keine_verbindung_aus_dem_pool(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Das Einreihen laeuft ueber die Verbindung des Clients (`connection=`).

    Der Pool wird fuer die Dauer des Einreihens gesperrt: Holte das Einreihen sich
    eine Verbindung daraus, schluege der Test an. Das laesst sich nur gegen echtes
    Postgres pruefen, weil Procrastinates `InMemoryConnector` `connection=`
    uebergeht.
    """
    _worker_am_leben(db_conn)

    def verboten(*_: Any, **__: Any) -> Any:
        raise AssertionError("Das Einreihen darf keine Pool-Verbindung holen.")

    monkeypatch.setattr(offener_client._pool, "connection", verboten)
    monkeypatch.setattr(offener_client._pool, "getconn", verboten)

    h = _abschicken(offener_client, "a.png")

    anzahl = db_conn.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE queueing_lock = %s", (h,)
    ).fetchone()
    assert anzahl is not None and anzahl[0] == 1


def test_eingereihter_job_liegt_in_der_queue_des_daemons(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any]
) -> None:
    """Task-Name und Queue des Jobs sind die Konstanten, unter denen der Daemon
    seinen Task anmeldet. In `default` waere er unsichtbar: kein Fehler, keine
    Meldung, der Job bliebe liegen."""
    _worker_am_leben(db_conn)
    h = _abschicken(offener_client, "a.png")

    zeile = db_conn.execute(
        "SELECT task_name, queue_name, lock, queueing_lock FROM procrastinate_jobs"
    ).fetchone()
    assert zeile == (
        config.TASK_OCR_DOCUMENT,
        config.QUEUE_OCR,
        h,
        h,
    )


def _procrastinate_zustand(klient: OCRClient) -> tuple[object, object]:
    """Pool und App des Clients. Eine Funktion und nicht das Attribut selbst,
    damit die Typpruefung ein spaeteres Lesen nicht aus einem frueheren
    `assert` ableitet."""
    return (klient._pool, klient._app)


def test_austritt_schliesst_pool_und_app(
    _test_datenbank: str, runpod: RunPodAntworten
) -> None:
    """Der Client legt seinen Pool selbst an und schliesst ihn selbst:
    `app.close()` schliesst einen uebergebenen Pool nicht."""
    klient = OCRClient(db_dsn=_test_datenbank, api_key="erfunden", preflight=False)
    assert _procrastinate_zustand(klient) == (None, None)

    with klient:
        pool = klient._pool
        assert pool is not None
        offen_beim_betreten = not pool.closed
        assert klient._app is not None

    assert offen_beim_betreten
    assert pool.closed
    assert _procrastinate_zustand(klient) == (None, None)


def test_image_mit_digest_wird_nach_seinem_tag_beurteilt(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`repo:tag@sha256:…` hat den Tag `tag`; der Digest ist nicht der Tag. Ein
    Image nur mit Digest hat keinen Tag und ist nicht pruefbar."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")
    digest = "@sha256:" + "ab" * 32

    runpod.marke = "registry.runpod.net/k-r-dockerfile:neu12345" + digest
    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", image="registry.runpod.net/k-r-dockerfile:neu12345" + digest)]
    )
    befund = _pruefen(_test_datenbank)
    assert _befund(befund, "image_tag")["bewertung"] == "in_ordnung"
    assert _befund(befund, "worker_image")["bewertung"] == "in_ordnung"

    runpod.endpoint = _guter_endpoint(
        workers=[_worker("w1", image="registry.runpod.net/k-r-dockerfile" + digest)]
    )
    punkt = _befund(_pruefen(_test_datenbank), "worker_image")
    assert punkt["bewertung"] == "nicht_pruefbar"
    assert punkt["ist"] == {"w1": None}


def test_template_image_ohne_tag_ist_nicht_pruefbar(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wie beim Worker: Ein Image, aus dem sich kein Tag lesen laesst, ist weder
    richtig noch falsch."""
    _worker_am_leben(db_conn)
    monkeypatch.setenv("MINERU_OCR_EXPECTED_IMAGE_TAG", "neu12345")
    runpod.marke = "registry.runpod.net:5000/k-r"
    punkt = _befund(_pruefen(_test_datenbank), "image_tag")
    assert punkt["bewertung"] == "nicht_pruefbar"


def test_konfigurierte_geheimnisvariable_wird_nicht_im_klartext_gezeigt(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Wer eine Variable wie `RUNPOD_AI_API_KEY` in `[endpoint_env]` nennt, will
    sie geprueft haben und nicht ausgegeben: Weder der Befund noch die Meldung
    des Abbruchs zeigt ihren Wert, weder Soll noch Ist."""
    datei = tmp_path / "config.toml"
    datei.write_text('[endpoint_env]\nRUNPOD_AI_API_KEY = "soll-nicht-ausgeben"\n')
    monkeypatch.setenv("MINERU_OCR_CONFIG", str(datei))
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(workers=[_worker("w1")])

    befund = _pruefen(_test_datenbank)
    punkt = _befund(befund, "env:RUNPOD_AI_API_KEY")
    assert punkt["bewertung"] == "abweichung"
    assert GEHEIMNIS not in repr(befund)
    assert "soll-nicht-ausgeben" not in repr(befund)

    with pytest.raises(ConfigDriftError) as fehler:
        _pruefen(_test_datenbank, strict=True)
    assert GEHEIMNIS not in str(fehler.value)
    assert "soll-nicht-ausgeben" not in str(fehler.value)


def test_geheimnisvariable_mit_richtigem_wert_ist_in_ordnung_und_bleibt_verdeckt(
    _test_datenbank: str,
    db_conn: psycopg.Connection[Any],
    runpod: RunPodAntworten,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verdeckt wird nur die Anzeige: Verglichen wird der Wert. Ein Worker ohne
    die Variable bleibt als Fehlen sichtbar, einer ohne Umgebung als nicht
    angegeben."""
    datei = tmp_path / "config.toml"
    datei.write_text(f'[endpoint_env]\nRUNPOD_AI_API_KEY = "{GEHEIMNIS}"\n')
    monkeypatch.setenv("MINERU_OCR_CONFIG", str(datei))
    _worker_am_leben(db_conn)

    runpod.endpoint = _guter_endpoint(workers=[_worker("w1")])
    punkt = _befund(_pruefen(_test_datenbank), "env:RUNPOD_AI_API_KEY")
    assert punkt["bewertung"] == "in_ordnung"
    assert punkt["soll"] == "***"
    assert punkt["ist"] == {"w1": "***"}

    ohne = _worker("w2")
    del ohne["env"]["RUNPOD_AI_API_KEY"]
    sonst = _worker("w3")
    del sonst["env"]
    runpod.endpoint = _guter_endpoint(workers=[ohne, sonst])
    punkt = _befund(_pruefen(_test_datenbank), "env:RUNPOD_AI_API_KEY")
    assert punkt["ist"] == {"w2": None, "w3": "(nicht angegeben)"}


def test_worker_mit_leerer_umgebung_ist_nicht_pruefbar(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Eine leere `env` sagt nichts ueber die Variablen: Sie ist wie eine fehlende
    „nicht pruefbar" und kein Abbruch."""
    _worker_am_leben(db_conn)
    leer = _worker("w1")
    leer["env"] = {}
    runpod.endpoint = _guter_endpoint(workers=[leer])

    befund = _pruefen(_test_datenbank, strict=True)  # wirft NICHT
    punkt = _befund(befund, "env:MINERU_MAX_CONCURRENCY")
    assert punkt["bewertung"] == "nicht_pruefbar"
    assert punkt["ist"] == {"w1": "(nicht angegeben)"}
    assert befund["drift"] == 0
