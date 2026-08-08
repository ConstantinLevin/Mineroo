"""Gemeinsame Vorrichtungen fuer alle Tests.

Zur Lage der Tests in Phase A: Jeder Test hier hat einen NAMEN und eine
Beschreibung, aber noch keinen Koerper. Das ist Absicht — ein Testname ist die
ausfuehrbare Fassung einer Zusicherung aus dem Vertrag. Solange die Koerper
fehlen, ist die Liste eine Aufgabenliste; sobald sie da sind, ist sie der Beweis.
Uebersprungene Tests werden von pytest gezaehlt und angezeigt, es kann also
keiner vergessen werden.

Was hier NICHT steht: eine Vorrichtung, die still eine Datenbank anlegt oder
einen Endpoint anspricht. Tests mit `db`-Kennzeichnung brauchen die laufende
Postgres-Instanz und sagen es; Tests mit `live`-Kennzeichnung kosten Geld und
laufen nur, wenn man sie ausdruecklich anfordert.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from mineru_ocr.config import Settings

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "2026-08-05-doku-verifikationstest"
"""Die sechs konservierten ECHTEN Antworten vom Doku-Verifikationstest.

Sie stammen aus Laeufen mit `transport: "inline"` und tragen deshalb den
Antwort-Umschlag, `markdown`, `content_list` mit `page_idx` und `middle` — aber
KEIN `tarball_b64`. Fuer den Tarball-Pfad, den der Betrieb faehrt, siehe
`TARBALL_FIXTURE`.

Eine Einschraenkung, die man kennen muss: ALLE SECHS SIND EINSEITIGE BILDER
(`blackscholes__-01.png` bis `-12.png`), ihre `page_idx` sind durchweg 0. Fuer
die Mehr-Teilstueck-Lage — Seitenversatz beim Zusammensetzen, Bild-Vereinigung
ueber Teilstuecke hinweg — gibt es hier also kein Material; jene Tests laufen
gegen erfundene Antworten. Der Seitenversatz ist inzwischen am 08.08.2026 in
einem echten Lauf ueber drei Teilstuecke geprueft worden (12 Seiten, `page_idx`
lueckenlos 0 bis 11), aber jener Lauf ist nicht als Fixture konserviert: `raw`
haelt das ZUSAMMENGESETZTE Ergebnis, nicht die einzelnen Antworten.
"""

TARBALL_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "artifacts"
    / "2026-08-08-erster-tarball-lauf"
    / "gute_nacht.tarball.response.json"
)
"""Die erste ECHTE `tarball_b64`-Antwort — aufgenommen am 08.08.2026.

WOZU SIE DA IST: Der Betrieb versendet `tarball_b64`, und bis zu diesem Tag gab
es dafuer KEIN Testmaterial. Der Auspacker fuer den Produktionspfad war damit
gegen eine Vorstellung gebaut und gegen nichts geprueft — der teuerste Zustand,
den ein Modul haben kann, weil er sich nicht von einem geprueften unterscheiden
laesst.

WAS SIE UMGEWORFEN HAT, gleich beim ersten Blick hinein:

    Das Archiv enthaelt `doc_content_list.json` UND `doc_content_list_v2.json`.
    BEIDE treffen dasselbe Zuordnungsmuster. Die Dokumentation beschrieb die
    zwei Namen als zwei Generationen DESSELBEN Formats mit Rueckfall; gemessen
    sind es zwei VERSCHIEDENE Formate, die nebeneinander liegen. Die flache
    Fassung fuehrt 13 Bloecke mit `text`/`text_level` und `page_idx`; die v2
    ist nach Seiten geschachtelt, unterscheidet `title`/`paragraph`/`table`
    ausdruecklich und traegt `table_type`, `table_nest_level` und
    `title_content.level` — Strukturinformation, die die flache nicht hat.

    Es gibt KEIN `span`-PDF. Der Vertrag von `ChunkArtifacts.span_pdf` sagt,
    die Span-Visualisierung komme mit dem Tarball. In dieser Antwort ist sie
    nicht enthalten.

    Es gibt KEIN `model.json`.

Der vollstaendige Archiv-Inhalt, wie er wirklich heisst: `doc.md`,
`doc_content_list.json`, `doc_content_list_v2.json`, `doc_layout.pdf`,
`doc_middle.json`, `images/<inhaltshash>.jpg`.

Die Antwort ist die eines EINSEITIGEN PDF. Fuer die Mehr-Teilstueck-Lage im
Tarball-Format gibt es weiterhin kein Fixture."""

TEST_DSN = "host=localhost port=5437 dbname=mineru_ocr_test user=jurgen"
"""Eigene Datenbank auf der laufenden Instanz. NICHT `mineru_ocr` und schon gar
nicht `gbrain` — ein Test, der die Arbeitsdatenbank leert, tut das genau einmal
und dann nie wieder, weil danach niemand mehr Tests laufen laesst."""


@pytest.fixture
def real_responses() -> list[dict[str, Any]]:
    """Die sechs konservierten Antworten, vollstaendig geladen."""
    return [
        json.loads(p.read_text())
        for p in sorted(FIXTURE_DIR.glob("*.response.json"))
    ]


@pytest.fixture
def echte_tarball_antwort() -> dict[str, Any]:
    """Die echte `tarball_b64`-Antwort vom 08.08.2026, vollstaendig geladen.

    Siehe `TARBALL_FIXTURE` fuer das, was sie ueber den Produktionspfad gezeigt
    hat — insbesondere den Doppeltreffer auf dem `_content_list`-Muster.
    """
    geladen: dict[str, Any] = json.loads(TARBALL_FIXTURE.read_text())
    return geladen


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Eine Einstellung mit Testwerten und ohne echten Schluessel.

    Der Schluessel ist ausdruecklich erfunden und als solcher lesbar. Das ist
    keine Kosmetik: Faende ein Test doch den Weg ins Netz, soll die Ablehnung
    von der Gegenseite kommen und nicht ein bezahlter Lauf.

    Das Spool-Verzeichnis liegt unter dem Temporaerpfad des jeweiligen Tests und
    ist damit je Test ein anderes — die echte Voreinstellung ist
    `~/.mineru_ocr/spool/`, und ein Test, der dort hineinschreibt, hinterlaesst
    Dateien im Arbeitsverzeichnis des Benutzers.

    Die uebrigen Werte bleiben die Voreinstellungen aus `Settings`, damit die
    Tests gegen das laufen, was im Betrieb wirklich gilt. Wo ein Test eine
    andere Schwelle braucht — kleinere Chunks, kleinere Payload —, baut er sie
    sich mit `dataclasses.replace` aus dieser Vorrichtung, statt hier einen
    zweiten Satz Werte einzufuehren, der mit dem Betrieb auseinanderlaeuft.
    """
    return Settings(
        db_dsn=TEST_DSN,
        endpoint_id="testendpoint",
        api_key="erfundener-schluessel-nicht-echt",
        spool_dir=tmp_path / "spool",
    )


@pytest.fixture(scope="session")
def _test_datenbank() -> str:
    """Legt die Test-Datenbank EINMAL je Sitzung an und gibt ihre DSN zurueck.

    Einmal je Sitzung und nicht je Test, weil `setup` die Verbindung mehrfach
    oeffnet und Procrastinates Schema anlegt — das je Test zu wiederholen kostet
    Sekunden und bringt nichts: Die Isolation zwischen den Tests stellt `db_conn`
    her, indem es die Zeilen leert, nicht indem es das Schema neu baut.

    Ist die Instanz auf Port 5437 nicht da, werden die Tests uebersprungen und
    nicht als Fehlschlag gezaehlt. Ein fehlender Datenbankserver ist keine
    Aussage ueber den Code.
    """
    from mineru_ocr.db import setup
    from mineru_ocr.errors import ConfigurationError, DatabaseError

    try:
        setup(TEST_DSN)
    except (ConfigurationError, DatabaseError) as fehler:
        pytest.skip(f"Postgres auf Port 5437 nicht verfuegbar: {fehler}")
    return TEST_DSN


@pytest.fixture
def db_conn(_test_datenbank: str) -> Iterator[Any]:
    """Eine Verbindung zur Test-Datenbank, mit LEEREN Tabellen je Test.

    Geleert werden `documents` und Procrastinates Job-Tabellen, damit kein Test
    die Zeilen eines anderen sieht. `TRUNCATE` und nicht `DELETE`, weil es die
    Zaehler zuruecksetzt und schneller ist.

    Die Datenbank heisst `mineru_ocr_test` (siehe TEST_DSN) — ausdruecklich nicht
    `mineru_ocr` und schon gar nicht `gbrain`. Ein Test, der die Arbeitsdatenbank
    leert, tut das genau einmal, und danach laesst niemand mehr Tests laufen.
    """
    from mineru_ocr.db import connect

    with connect(_test_datenbank) as conn:
        conn.execute(
            "TRUNCATE documents, procrastinate_events, procrastinate_jobs, "
            "procrastinate_periodic_defers, procrastinate_workers "
            "RESTART IDENTITY CASCADE"
        )
        yield conn


@pytest.fixture
async def async_conn(_test_datenbank: str) -> AsyncIterator[Any]:
    """Die asynchrone Verbindung — die Seite, auf der der Daemon schreibt.

    Eine EIGENE Verbindung neben `db_conn`, nicht dieselbe: Das Terminal-
    Schreiben und das Warten darauf muessen sich in den Tests genauso verhalten
    wie im Betrieb, und dort sind es zwei Prozesse. Auf einer geteilten
    Verbindung waere das Wecksignal ein Selbstgespraech, und ein Test, der
    dieselbe Verbindung benutzt, koennte einen fehlenden `NOTIFY` gar nicht
    bemerken.

    Das Leeren macht `db_conn`; diese Vorrichtung raeumt nicht selbst auf, sonst
    leerten zwei Vorrichtungen dieselbe Tabelle in unbestimmter Reihenfolge.
    """
    from mineru_ocr.db import connect_async

    conn = await connect_async(_test_datenbank)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def mock_runpod() -> Any:
    """Ein RunPod-Zugang mit vorgegebenen Antworten, ohne Netz und ohne Kosten.

    Wird in Phase B gefuellt: ein `httpx.MockTransport`, der Absenden und
    Abfragen bedient und dabei die konservierten Antworten ausliefert. Der
    Transport ist die Naht, an der das Netz endet — `RunPodClient` nimmt einen
    fertigen HTTP-Client entgegen, genau dafuer.
    """
    pytest.skip("Vorrichtung folgt in Phase B")
