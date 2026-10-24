"""Die Fassade — Preflight, Einreihen, Warten, Nachsehen."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import NullConnectionPool

from mineru_ocr import client as client_modul
from mineru_ocr import config, tasks
from mineru_ocr.client import HASH_FORM, OCRClient
from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, write_terminal_async
from mineru_ocr.errors import ConfigDriftError, ConfigurationError, DocumentNotFound
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
        "id": "l79lebhinxaqjz",
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


def _huelle(client: OCRClient, tmp_path: Path, name: str) -> OCRWrapper:
    pfad = tmp_path / name
    pfad.write_bytes(PNG_KOPF + name.encode())
    return OCRWrapper.from_path(pfad, client.conn, client.settings)


def _worker_am_leben(conn: psycopg.Connection[Any]) -> None:
    conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")


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


def test_nicht_pruefbar_ist_nicht_in_ordnung(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Laeuft kein Worker, sind dessen Umgebungsvariablen NICHT pruefbar — und
    der Befund sagt das, statt stillschweigend „in Ordnung" zu melden. Genau
    darin liegt der Wert dieser Pruefung."""
    _worker_am_leben(db_conn)
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False
    ) as klient:
        befund = klient.preflight()

    umgebung = [b for b in befund["checks"] if b["punkt"] == "worker_env"][0]
    assert umgebung["bewertung"] == "nicht_pruefbar"
    assert umgebung["bewertung"] != "in_ordnung"


def test_laufender_worker_macht_die_umgebung_pruefbar(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Gegenprobe: Mit einem laufenden Worker IST sie pruefbar."""
    _worker_am_leben(db_conn)
    runpod.endpoint = _guter_endpoint(
        workers=[{"id": "wrk", "env": {"MINERU_MAX_CONCURRENCY": "1"}}]
    )
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False
    ) as klient:
        befund = klient.preflight()

    umgebung = [b for b in befund["checks"] if b["punkt"] == "worker_env"][0]
    assert umgebung["bewertung"] == "in_ordnung"


def test_altes_image_ist_warnung_kein_abbruch(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Mit altem Image kann man arbeiten, man soll es nur wissen."""
    _worker_am_leben(db_conn)
    runpod.marke = "registry.runpod.net/…-dockerfile:17d4955b2"
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False, strict=True
    ) as klient:
        befund = klient.preflight()  # wirft NICHT

    marke = [b for b in befund["checks"] if b["punkt"] == "image_tag"][0]
    assert marke["bewertung"] == "warnung"
    assert befund["drift"] == 0


def test_rechenzentren_werden_gemeldet(
    _test_datenbank: str, db_conn: psycopg.Connection[Any], runpod: RunPodAntworten
) -> None:
    """Die Rechenzentren werden berichtet, wenn der Endpoint sie ausweist — und
    sonst gilt die Frage als OFFEN, nicht als beantwortet.

    Der Client richtet kein Verhalten danach; das ist eine Entscheidung des
    Menschen und kein Schalter im Programm. Umso wichtiger ist, dass die Zeile
    nicht so aussieht, als haette sie etwas festgestellt.

    WAS HIER FRUEHER STAND UND WARUM ES FALSCH WAR: Die Pruefung las
    `dataCenterIds` ODER, ersatzweise, `gpuTypeIds`. Der echte Endpoint weist
    keine `dataCenterIds` aus — im Lauf am 08.08.2026 meldete die Zeile deshalb
    `['NVIDIA RTX A5000', 'NVIDIA L4', 'NVIDIA GeForce RTX 3090', 'NVIDIA
    GeForce RTX 4090']` unter der Ueberschrift „EU fuer den Steuer-Bestand".
    Das sind GPU-Typen. Ausgerechnet die Pruefung, an der die Datenschutz-Frage
    haengt, sah ein Feld an, das die Frage nicht beantworten kann — und meldete
    `gemeldet`.

    Die Vorrichtung bildet den echten Schnappschuss ab (mit `gpuTypeIds`, ohne
    `dataCenterIds`). Der richtige Befund ist damit `nicht_pruefbar`, und dieser
    Test schreibt genau das fest.
    """
    _worker_am_leben(db_conn)
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False
    ) as klient:
        befund = klient.preflight()

    rechenzentren = [b for b in befund["checks"] if b["punkt"] == "datacenters"][0]
    assert "EU" in rechenzentren["soll"]
    # Der Endpoint weist keine Rechenzentren aus -> die Frage bleibt offen.
    assert rechenzentren["bewertung"] == "nicht_pruefbar"
    assert rechenzentren["ist"] is None

    # Und wenn er sie ausweist, werden sie gemeldet — und zwar SIE, nicht die
    # GPU-Typen.
    runpod.endpoint = _guter_endpoint(dataCenterIds=["EU-RO-1", "EUR-IS-1"])
    with OCRClient(
        db_dsn=_test_datenbank, api_key="erfunden", preflight=False
    ) as klient:
        befund = klient.preflight()
    rechenzentren = [b for b in befund["checks"] if b["punkt"] == "datacenters"][0]
    assert rechenzentren["bewertung"] == "gemeldet"
    assert rechenzentren["ist"] == ["EU-RO-1", "EUR-IS-1"]


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


def test_ocr_start_stirbt_bei_totem_daemon(
    offener_client: OCRClient, tmp_path: Path
) -> None:
    """Einreihen ohne laufenden Exekutor bricht ab, statt Jobs anzuhaeufen, die
    niemand abholt.

    Der Wunsch des Auftraggebers, woertlich: „ja natürlich der client soll dann
    sterben und errorn". Und mit `strict=False` ist es eine Warnung — Jobs
    anzuhaeufen ist zulaessig, der Daemon kann spaeter starten.
    """
    huelle = _huelle(offener_client, tmp_path, "a.png")
    with pytest.raises(ConfigurationError, match="kein Daemon"):
        offener_client.ocr_start(huelle)


# --- Arbeit ---------------------------------------------------------------


def test_ocr_start_blockiert_nicht(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Es reiht ein und kehrt zurueck — „das losschicken ist einfach einen job
    erstellen"."""
    _worker_am_leben(db_conn)
    huelle = _huelle(offener_client, tmp_path, "a.png")
    zurueck = offener_client.ocr_start(huelle)

    assert zurueck is huelle
    anzahl = db_conn.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE queueing_lock = %s",
        (huelle.sha256,),
    ).fetchone()
    assert anzahl is not None and anzahl[0] == 1


async def test_ocr_start_faesst_fertiges_nicht_an(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """Fertige Dokumente werden nicht eingereiht — sonst waere jedes erneute
    Durchlaufen eines Ordners ein vollstaendig neu bezahlter Bestand."""
    _worker_am_leben(db_conn)
    fertig = _huelle(offener_client, tmp_path, "fertig.png")
    offen = _huelle(offener_client, tmp_path, "offen.png")
    await write_terminal_async(
        async_conn, sha256=fertig.sha256, status=DocumentStatus.DONE, raw={}
    )
    fertig.refresh()

    offener_client.ocr_start(fertig)
    offener_client.ocr_start(offen)

    anzahl = db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone()
    assert anzahl is not None and anzahl[0] == 1


async def test_ocr_start_reiht_alles_ohne_endzustand_ein(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """Auch Zeilen, deren juengster Job endgueltig endete, ohne etwas zu
    schreiben: „es wird dann ncohmal"."""
    _worker_am_leben(db_conn)
    huelle = _huelle(offener_client, tmp_path, "nochmal.png")
    offener_client.ocr_start(huelle)
    # Der Job endet, ohne dass die Zeile geschrieben wird — harter Abschuss im
    # Schreibfenster. Die Zeile bleibt `new`.
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")
    assert huelle.refresh().status is DocumentStatus.NEW

    offener_client.ocr_start(huelle)
    anzahl = db_conn.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE queueing_lock = %s",
        (huelle.sha256,),
    ).fetchone()
    assert anzahl is not None and anzahl[0] == 2


async def test_wait_gibt_bei_jedem_endzustand_zurueck(
    offener_client: OCRClient,
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """Auch bei `failed` — das Scheitern ist ein Ergebnis, kein Fehler des
    Wartens. Der Aufrufer prueft `.success`."""
    huelle = _huelle(offener_client, tmp_path, "gescheitert.png")
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.FAILED,
        raw={"markdown": "Teil"},
        error="aus",
    )

    zurueck = offener_client.wait(huelle, timeout=5.0)
    assert zurueck.status is DocumentStatus.FAILED
    assert zurueck.success is False


def test_wait_auf_unbekannten_hash_wirft_sofort(offener_client: OCRClient) -> None:
    """Nicht erst nach Ablauf der Wartezeit. Auf etwas zu warten, das nie
    eingereiht wurde, ist immer ein Irrtum des Aufrufers."""
    with pytest.raises(DocumentNotFound):
        offener_client.wait("f" * 64, timeout=30.0)


async def test_get_braucht_keinen_pfad(
    offener_client: OCRClient,
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """`get("<hash>")` liefert die Huelle samt Ergebnis, ohne die Quelldatei
    anzufassen — sie kann laengst weg sein."""
    pfad = tmp_path / "verschwindet.png"
    pfad.write_bytes(PNG_KOPF + b"weg gleich")
    huelle = OCRWrapper.from_path(pfad, offener_client.conn, offener_client.settings)
    await write_terminal_async(
        async_conn,
        sha256=huelle.sha256,
        status=DocumentStatus.DONE,
        raw={"markdown": "steht in der Datenbank", "content_list": [], "images": {}},
    )
    pfad.unlink()

    ueber_hash = offener_client.get(huelle.sha256)
    assert ueber_hash["markdown"] == "steht in der Datenbank"


def test_hash_oder_pfad_wird_festgelegt_unterschieden(
    offener_client: OCRClient, tmp_path: Path
) -> None:
    """Die Regel steht als Muster an EINER Stelle und ist nicht geraten.

    Vierundsechzig Zeichen aus `[0-9a-f]` sind ein Hash, alles andere ist ein
    Pfad. Dieselbe Regel gilt in `get`, `document` und `wait` — drei Fassungen
    liefen frueher oder spaeter auseinander.
    """
    assert HASH_FORM.match("a" * 64)
    assert not HASH_FORM.match("a" * 63)
    assert not HASH_FORM.match("A" * 64)  # Grossbuchstaben sind kein sha256-Text
    assert not HASH_FORM.match("/tmp/x.pdf")

    pfad = tmp_path / "ueber_pfad.png"
    pfad.write_bytes(PNG_KOPF + b"p")
    huelle = OCRWrapper.from_path(pfad, offener_client.conn, offener_client.settings)
    assert offener_client.get(pfad).sha256 == huelle.sha256
    assert offener_client.get(huelle.sha256).sha256 == huelle.sha256


async def test_retry_failed_nimmt_nur_failed(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """`input_error` wird uebersprungen und nicht mitgezaehlt: Ein so
    wiedervorgelegter Job taete nichts, verbrauchte aber einen Versuch.

    Der Fall ist konkret: Ein zu grosses DOCX liesse sich damit scheinbar
    wiederholen, bliebe `input_error`, und beim naechsten echten Bedarf waere das
    Budget aufgebraucht.
    """
    _worker_am_leben(db_conn)
    gescheitert = _huelle(offener_client, tmp_path, "failed.png")
    eingabefehler = _huelle(offener_client, tmp_path, "input_error.png")
    offener_client.ocr_start(gescheitert)
    offener_client.ocr_start(eingabefehler)
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'failed'")

    await write_terminal_async(
        async_conn, sha256=gescheitert.sha256, status=DocumentStatus.FAILED, raw={}
    )
    await write_terminal_async(
        async_conn,
        sha256=eingabefehler.sha256,
        status=DocumentStatus.INPUT_ERROR,
        raw={},
    )
    gescheitert.refresh()
    eingabefehler.refresh()

    assert offener_client.retry_failed(eingabefehler) == 0
    assert offener_client.retry_failed(gescheitert) == 1


async def test_retry_failed_ueberspringt_aborted(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """Das Tooling weist `aborted` ab; der Heilungsweg ist erneutes Einreihen.
    Uebersprungen und gemeldet, nicht mit einer Ausnahme aufgehalten."""
    _worker_am_leben(db_conn)
    huelle = _huelle(offener_client, tmp_path, "abgebrochen.png")
    offener_client.ocr_start(huelle)
    db_conn.execute("UPDATE procrastinate_jobs SET status = 'aborted'")
    await write_terminal_async(
        async_conn, sha256=huelle.sha256, status=DocumentStatus.FAILED, raw={}
    )
    huelle.refresh()

    # Kein Absturz, sondern eine ehrliche Null.
    assert offener_client.retry_failed(huelle) == 0


# --- Nachsehen ------------------------------------------------------------


async def test_status_zaehlt_in_summen(
    offener_client: OCRClient,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """Zwei Abfragen, unabhaengig von der Bestandsgroesse — nicht eine je
    Dokument.

    Bei zehntausend Dokumenten waere die Einzelaufloesung zehntausend Abfragen
    fuer eine Uebersicht, die niemand Dokument fuer Dokument liest.
    """
    _worker_am_leben(db_conn)
    for nr in range(3):
        huelle = _huelle(offener_client, tmp_path, f"{nr}.png")
        offener_client.ocr_start(huelle)
    erste = _huelle(offener_client, tmp_path, "0.png")
    await write_terminal_async(
        async_conn, sha256=erste.sha256, status=DocumentStatus.DONE, raw={}
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
    tmp_path: Path,
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

        huelle = _huelle(klient, tmp_path, "a.png")
        klient.ocr_start(huelle)
        klient.document(huelle.sha256)
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
    tmp_path: Path,
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

    huelle = _huelle(offener_client, tmp_path, "a.png")
    offener_client.ocr_start(huelle)

    anzahl = db_conn.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE queueing_lock = %s",
        (huelle.sha256,),
    ).fetchone()
    assert anzahl is not None and anzahl[0] == 1


def test_eingereihter_job_liegt_in_der_queue_des_daemons(
    offener_client: OCRClient, db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Task-Name und Queue des Jobs sind die Konstanten, unter denen der Daemon
    seinen Task anmeldet. In `default` waere er unsichtbar: kein Fehler, keine
    Meldung, der Job bliebe liegen."""
    _worker_am_leben(db_conn)
    huelle = _huelle(offener_client, tmp_path, "a.png")
    offener_client.ocr_start(huelle)

    zeile = db_conn.execute(
        "SELECT task_name, queue_name, lock, queueing_lock FROM procrastinate_jobs"
    ).fetchone()
    assert zeile == (
        config.TASK_OCR_DOCUMENT,
        config.QUEUE_OCR,
        huelle.sha256,
        huelle.sha256,
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
