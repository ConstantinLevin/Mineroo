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
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from mineru_ocr.config import Settings

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "2026-08-05-doku-verifikationstest"
"""Die sechs konservierten ECHTEN Antworten vom Doku-Verifikationstest.

Sie stammen aus Laeufen mit `transport: "inline"` und tragen deshalb den
Antwort-Umschlag, `markdown`, `content_list` mit `page_idx` und `middle` — aber
KEIN `tarball_b64`. Fuer den Tarball-Pfad, den der Betrieb faehrt, siehe
`LEERE_SEITE_FIXTURE` und `TARBALL_FIXTURE` (diese ist derzeit entfernt, ihre
Tests laufen ausgesetzt).

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

AUSGESETZT, SOLANGE DIE DATEI FEHLT: Der Tarball trug den Text einer privaten
E-Mail zweier Personen (Dokument, Inhaltslisten und Layout-PDF) und ist vor dem
ersten Push aus dem Repository genommen worden (Mineroo#2). Die Tests, die ihn
lesen, werden uebersprungen — mit diesem Grund in der Ausgabe von `pytest -rs` —
bis eine neue Aufnahme aus Material ohne Personenbezug unter demselben Namen
liegt (Ticket zum MinerU-4-Vertrag). Die Zahlen unten beschreiben die entfernte
Aufnahme; die neue bekommt neue Zahlen.

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
Tarball-Format gab es `MEHRTEILIG_FIXTURE` — drei echte Antworten desselben
Dokuments, aufgenommen einen Tag spaeter und vor dem ersten Push entfernt
(siehe dort). Der Satz „dafuer gibt es weiterhin
kein Fixture" stand hier bis zum 10.08.2026 und war ab dem 09.08. falsch."""

TEST_DSN = "host=localhost port=5437 dbname=mineru_ocr_test user=jurgen"
"""Eigene Datenbank auf der laufenden Instanz. NICHT `mineru_ocr` und schon gar
nicht `gbrain` — ein Test, der die Arbeitsdatenbank leert, tut das genau einmal
und dann nie wieder, weil danach niemand mehr Tests laufen laesst."""


@pytest.fixture(autouse=True)
def _konfiguration_isolieren(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Haelt die Konfiguration des Entwicklers aus jedem Test heraus.

    Endpoint und Datenbank haben keine Voreinstellung im Code mehr; sie kommen
    aus Umgebungsvariablen oder der Konfigurationsdatei. Ohne diese Vorrichtung
    haengte das Ergebnis eines Tests davon ab, was in der Shell steht und ob
    unter `~/.mineru_ocr/config.toml` eine Datei liegt — und ein Test, der
    zufaellig den echten Endpoint oder die echte Datenbank einstellte, waere der
    teuerste, den es gibt.

    Eine vorhandene `MINERU_OCR_*`-Umgebung wird geraeumt, die Standarddatei auf
    einen nicht vorhandenen Pfad umgebogen, und Endpoint und Datenbank zeigen auf
    erfundene bzw. die Test-Datenbank. Tests, die die Quellen selbst pruefen,
    uebergeben `environ=` und `config_path=` ausdruecklich.
    """
    for name in [n for n in os.environ if n.startswith("MINERU_OCR_")]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(
        "mineru_ocr.config.DEFAULT_CONFIG_PATH", tmp_path / "keine-konfiguration.toml"
    )
    monkeypatch.setenv("MINERU_OCR_ENDPOINT_ID", "testendpoint")
    monkeypatch.setenv("MINERU_OCR_DB_DSN", TEST_DSN)


@pytest.fixture
def real_responses() -> list[dict[str, Any]]:
    """Die sechs konservierten Antworten, vollstaendig geladen."""
    return [
        json.loads(p.read_text())
        for p in sorted(FIXTURE_DIR.glob("*.response.json"))
    ]


LEERE_SEITE_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "artifacts"
    / "2026-08-08-erster-tarball-lauf"
    / "leere_seite.tarball.response.json"
)
"""Die echte Antwort auf eine LEERE A4-Seite — aufgenommen am 08.08.2026.

Sie beantwortet den Punkt, der im Plan ausdruecklich offenblieb und „vorm bauen"
haette geklaert werden sollen: wie die Schnittstelle auf eine leere Seite
antwortet. Die Antwort lautet: mit `COMPLETED` und `ok: true` — eine leere Seite
ist fuer die Engine ein Erfolg. Das Archiv traegt `doc.md` mit 0 Bytes,
`doc_content_list.json` mit `[]`, `doc_content_list_v2.json` mit `[[]]`, ein
`doc_middle.json` mit leeren Blocklisten und — bemerkenswert — trotzdem ein
`doc_layout.pdf` von 462 Bytes. Ein Layout-PDF ist also kein Hinweis auf Inhalt.

Diese eine Antwort hat zwei Fehler ans Licht gebracht, die einander verdeckten:
eine Belegt-Pruefung, die den Wahrheitswert statt der Anwesenheit fragte (eine
gueltige leere Liste galt als „Platz frei", und die v2-Fassung ueberschrieb die
flache), und eine Vollstaendigkeitspruefung, die eine echt leere Seite fuer ein
kaputtes Archiv gehalten haette — was aus dem gueltigen `empty` ein `failed`
gemacht haette, mit Wiedervorlage und erneutem Bezahlen."""


MEHRTEILIG_FIXTURE: Final = [
    (
        (0, 4),
        Path(__file__).resolve().parent.parent
        / "artifacts"
        / "2026-08-08-erster-tarball-lauf"
        / "mehrteilig_0_seiten_0-4.response.json",
    ),
    (
        (5, 9),
        Path(__file__).resolve().parent.parent
        / "artifacts"
        / "2026-08-08-erster-tarball-lauf"
        / "mehrteilig_1_seiten_5-9.response.json",
    ),
    (
        (10, 11),
        Path(__file__).resolve().parent.parent
        / "artifacts"
        / "2026-08-08-erster-tarball-lauf"
        / "mehrteilig_2_seiten_10-11.response.json",
    ),
]
"""Drei ECHTE Tarball-Antworten EINES Dokuments — die drei Teilstuecke eines
12-seitigen Dokuments, aufgenommen am 09.08.2026.

AUSGESETZT, SOLANGE DIE DATEIEN FEHLEN: Die Aufnahmen trugen den Inhalt eines
Steuerdokuments mit personenbezogenen Daten und sind vor dem ersten Push aus
dem Repository genommen worden (Mineroo#2). Die Tests, die sie lesen, werden
uebersprungen — mit diesem Grund in der Ausgabe von `pytest -rs` — bis neue
Aufnahmen aus Material ohne Personenbezug unter denselben Namen liegen (Ticket
zum MinerU-4-Vertrag, Spec Mineroo#1, Abschnitt „Testing Decisions"). Die
Zahlen und Zusicherungen unten beschreiben die entfernten Aufnahmen; die neuen
bekommen neue Zahlen.

DIE LUECKE, DIE SIE SCHLIESST, war die groesste des Testmaterials und stand
lange ausdruecklich benannt hier: Fuer die Mehr-Teilstueck-Lage gab es kein
echtes Material, also lief der Seitenversatz beim Zusammensetzen — die
fehleranfaelligste Rechnung des ganzen Pakets — gegen erfundene Antworten.

WAS SIE BEWEISEN, und was vorher nur eine Annahme war: JEDES Teilstueck traegt
`page_idx` LOKAL, beginnend bei 0. Gemessen: Teilstueck 0 (Originalseiten 0-4)
hat `page_idx` 0 bis 4, Teilstueck 1 (Originalseiten 5-9) ebenfalls 0 bis 4,
Teilstueck 2 (Originalseiten 10-11) hat 0 und 1. Ohne die Versatz-Korrektur
traege das Dokument dreimal eine „Seite 0" — und weil die Liste dabei
vollstaendig und plausibel aussieht, faellt es niemandem auf.

Nach dem Zusammensetzen: 85 Bloecke, `page_idx` lueckenlos 0 bis 11. Dieselbe
Zahl wie im Lauf durch das Tool am 08.08.2026 — die Fixtures und der echte
Durchlauf stimmen ueberein.

Ein weiterer Befund nebenbei: Auch hier traegt JEDES Teilstueck sein eigenes
`doc_content_list_v2.json` in `extras`. Der Doppeltreffer ist nicht an einzelne
Antworten gebunden, sondern die Regel."""

FEHLERANTWORT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "artifacts"
    / "2026-08-08-erster-tarball-lauf"
    / "fehlerantwort.response.json"
)
"""Eine echte `FAILED`-Antwort des Workers — erzwungen am 09.08.2026.

Erzeugt, indem ein GUELTIGES PDF mit einem ungueltigen Backend-Namen geschickt
wurde: Die lokalen Pruefungen haetten daran nichts zu beanstanden, der Fehler
entsteht ausschliesslich auf der Gegenseite. Genau die Lage, die
`executor._endzustand_uebersetzen` bewerten muss und fuer die es kein Material
gab.

WAS SIE ZEIGT — und was mehr ist, als der Code bis dahin annahm:

    `status: "FAILED"` und, auf OBERSTER Ebene, ein `error` mit der Meldung
    im Klartext.

    Ein vollstaendiges `output` — eine Fehlerantwort ist also NICHT leer. Darin
    `ok: false`, der `debug`-Block mit GPU, Modellverzeichnis und Phasenzeiten,
    und ein kompletter `traceback` aus dem Worker.

Der Traceback ist die brauchbarste Diagnose, die es zu einem gescheiterten
Dokument gibt. Er war vorher nur als Python-Textdarstellung in
`raw["failure"]["message"]` vorhanden, weil `_endzustand_uebersetzen` vor dem
Schutzblock stand; jetzt steht die ganze Antwort als abfragbares JSON unter
`raw["unverwertbare_antworten"]`.

Und die Antwort hat noch etwas geliefert, das nirgends dokumentiert war: die
Liste der gueltigen Backend-Namen. Der Worker nennt sie selbst, wenn man ihn
falsch fragt — sie steht jetzt in `config.GUELTIGE_BACKENDS` und wird lokal
geprueft, statt 177 Sekunden Kaltstart je Dokument zu kosten."""


def _ohne_aufnahme_ueberspringen(grund: str, pfade: list[Path]) -> None:
    """Ueberspringt den anfordernden Test, wenn eine der Aufnahmen fehlt.

    `grund` ist der Anfang des Skip-Grunds („Aufnahme … fehlt"); angehaengt
    werden die Herkunft der Luecke und die fehlenden Dateinamen.

    Die Aufnahmen, um die es geht, sind vor dem ersten Push entfernt worden,
    weil sie personenbezogene Daten trugen (Mineroo#2). Der Grund steht in der
    Ausgabe von `pytest -rs`, samt den fehlenden Dateinamen: Die Ersatzaufnahmen
    muessen unter GENAU diesen Namen liegen, sonst wird der Test weiter
    uebersprungen, ohne je zu scheitern.
    """
    fehlend = [pfad.name for pfad in pfade if not pfad.exists()]
    if fehlend:
        pytest.skip(
            f"{grund} (personenbezogen, vor dem ersten Push entfernt, Mineroo#2); "
            f"Ersatz kommt mit dem Ticket zum MinerU-4-Vertrag: {', '.join(fehlend)}"
        )


@pytest.fixture
def echte_mehrteilige_antworten() -> list[tuple[tuple[int, int], dict[str, Any]]]:
    """Die drei echten Teilstueck-Antworten samt ihrem Seitenbereich.

    Fehlen die Dateien (siehe `MEHRTEILIG_FIXTURE`), ist jeder Test, der diese
    Vorrichtung anfordert, uebersprungen — nicht bestanden und nicht gescheitert.
    """
    _ohne_aufnahme_ueberspringen(
        "Aufnahmen der mehrteiligen Antwort fehlen",
        [pfad for _, pfad in MEHRTEILIG_FIXTURE],
    )
    return [
        (bereich, json.loads(pfad.read_text()))
        for bereich, pfad in MEHRTEILIG_FIXTURE
    ]


@pytest.fixture
def echte_fehlerantwort() -> dict[str, Any]:
    """Die echte FAILED-Antwort, vollstaendig geladen."""
    geladen: dict[str, Any] = json.loads(FEHLERANTWORT_FIXTURE.read_text())
    return geladen


@pytest.fixture
def echte_leere_antwort() -> dict[str, Any]:
    """Die echte Antwort auf eine leere Seite, vollstaendig geladen."""
    geladen: dict[str, Any] = json.loads(LEERE_SEITE_FIXTURE.read_text())
    return geladen


@pytest.fixture
def echte_tarball_antwort() -> dict[str, Any]:
    """Die echte `tarball_b64`-Antwort vom 08.08.2026, vollstaendig geladen.

    Siehe `TARBALL_FIXTURE` fuer das, was sie ueber den Produktionspfad gezeigt
    hat — insbesondere den Doppeltreffer auf dem `_content_list`-Muster.

    Fehlt die Datei (siehe `TARBALL_FIXTURE`), ist jeder Test, der diese
    Vorrichtung anfordert, uebersprungen — nicht bestanden und nicht gescheitert.
    """
    _ohne_aufnahme_ueberspringen("Aufnahme der Tarball-Antwort fehlt", [TARBALL_FIXTURE])
    geladen: dict[str, Any] = json.loads(TARBALL_FIXTURE.read_text())
    return geladen


@pytest.fixture
def settings() -> Settings:
    """Eine Einstellung mit Testwerten und ohne echten Schluessel.

    Der Schluessel ist ausdruecklich erfunden und als solcher lesbar. Das ist
    keine Kosmetik: Faende ein Test doch den Weg ins Netz, soll die Ablehnung
    von der Gegenseite kommen und nicht ein bezahlter Lauf.

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

    Geleert werden `documents`, `inputs` und Procrastinates Job-Tabellen, damit kein Test
    die Zeilen eines anderen sieht. `TRUNCATE` und nicht `DELETE`, weil es die
    Zaehler zuruecksetzt und schneller ist.

    Die Datenbank heisst `mineru_ocr_test` (siehe TEST_DSN) — ausdruecklich nicht
    `mineru_ocr` und schon gar nicht `gbrain`. Ein Test, der die Arbeitsdatenbank
    leert, tut das genau einmal, und danach laesst niemand mehr Tests laufen.
    """
    from mineru_ocr.db import connect

    with connect(_test_datenbank) as conn:
        conn.execute(
            "TRUNCATE documents, inputs, procrastinate_events, procrastinate_jobs, "
            "procrastinate_periodic_defers, procrastinate_workers "
            "RESTART IDENTITY CASCADE"
        )
        yield conn


@pytest.fixture
def abgestuerzter_worker(db_conn: Any) -> Callable[[], None]:
    """Legt die Zeile an, die ein abgestuerzter Daemon hinterlaesst.

    Sie bleibt bis zum naechsten Start stehen, und ihr Alter ist das Merkmal:
    67 Tage, weit ueber der Schwelle (`config.DAEMON_HEARTBEAT_MAX_AGE_SECONDS`).
    Eine Funktion und kein sofortiges Anlegen, weil Tests die Worker-Tabelle vorher
    leeren oder die Zeile erst mitten im Ablauf brauchen.
    """

    def anlegen() -> None:
        db_conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) "
            "VALUES (now() - interval '67 days')"
        )

    return anlegen


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


# EINE VORRICHTUNG STAND HIER UND WURDE VON KEINEM TEST BENUTZT: `mock_runpod`,
# ein Platzhalter aus Phase A mit `pytest.skip("Vorrichtung folgt in Phase B")`
# im Koerper. Phase B ist gebaut, und sie hat den Platzhalter nicht gebraucht —
# `tests/test_runpod.py` setzt einen `httpx.MockTransport` unmittelbar ein, und
# `tests/test_executor.py` benutzt eine eigene Umsetzung des `RunPodAccess`-
# Protokolls. Beides ist naeher an der jeweiligen Naht als eine gemeinsame
# Vorrichtung es waere.
#
# Entfernt statt stehengelassen: Eine Vorrichtung, die aussieht, als gaebe es
# einen vorgesehenen Weg, den aber niemand geht, kostet beim naechsten Leser
# genau die Minute, die er braucht, um festzustellen, dass sie nichts tut.
