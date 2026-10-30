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

import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import ersatz_antworten as ersatz
from mineru_ocr.config import Settings

# Die aelteren echten Aufnahmen unter `artifacts/` stammen von MinerU 3.4.
#
# Mineroo#11 hat das Paket auf den MinerU-4-Vertrag umgestellt. Die 3.4-Antworten
# (`2026-08-05-doku-verifikationstest/`, `2026-08-08-erster-tarball-lauf/`) tragen
# Dateien, die es unter MinerU 4 nicht mehr gibt (`content_list`, `doc.md`,
# `doc_middle.json`), und werden deshalb nicht mehr abgespielt. An ihrer Stelle
# spielen die Tests die ECHTEN Antworten des MinerU-4-Endpoints
# (`artifacts/2026-10-26-mineru-4.0.12-endpoint/`, `ersatz_antworten.ECHT_ENDPOINT`,
# aufgenommen in `advanced` mit OCR) und, fuer Lagen, die sich nicht gezielt
# hervorrufen lassen, Ersatz-Antworten aus `ersatz_antworten`, gebaut nach den
# Quellen, die die MinerU-4-Antwort festlegen.

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

    Eine vorhandene `MINERU_OCR_*`-Umgebung wird geraeumt, TOML und feste .env
    auf nicht vorhandene Pfade umgebogen. Endpoint und Datenbank zeigen auf
    erfundene bzw. die Test-Datenbank. Tests, die die Quellen selbst pruefen,
    uebergeben `environ=`, `config_path=` bzw. `env_path=` ausdruecklich.
    """
    for name in [n for n in os.environ if n.startswith("MINERU_OCR_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(
        "mineru_ocr.config.DEFAULT_CONFIG_PATH", tmp_path / "keine-konfiguration.toml"
    )
    monkeypatch.setattr(
        "mineru_ocr.config.DEFAULT_ENV_PATH", tmp_path / "keine-umgebung.env"
    )
    monkeypatch.setenv("MINERU_OCR_ENDPOINT_ID", "testendpoint")
    monkeypatch.setenv("MINERU_OCR_DB_DSN", TEST_DSN)
    # Auch der RunPod-Schluessel des Entwicklers bleibt draussen: Wer einen
    # braucht, uebergibt ihn ausdruecklich. Sonst hinge davon, ob
    # `~/.runpod/config.toml` auf der Maschine liegt, ob ein Test besteht.
    monkeypatch.setattr(
        "mineru_ocr.config.RUNPOD_CONFIG_PATH", tmp_path / "kein-runpod-schluessel.toml"
    )


@pytest.fixture
def echte_fehlerantwort() -> dict[str, Any]:
    """Eine echte `FAILED`-Antwort des MinerU-4-Workers, erzwungen am 26.10.2026
    (`ersatz_antworten.ECHT_ENDPOINT`, Herkunft im `README.md` des Ordners).

    Erzeugt, indem ein GUELTIGES PDF mit der Stufe `gibt-es-nicht` geschickt
    wurde: Die lokalen Pruefungen haetten daran nichts zu beanstanden, der Fehler
    entsteht ausschliesslich auf der Gegenseite (`ValueError: input validation
    failed: tier must be one of …`). Genau die Lage, die
    `executor._endzustand_uebersetzen` bewerten muss.

    WAS SIE ZEIGT: `status: "FAILED"` und, auf OBERSTER Ebene, ein `error` mit der
    Meldung im Klartext; dazu ein vollstaendiges `output` — eine Fehlerantwort ist
    also NICHT leer — mit `ok: false`, dem `debug`-Block mit GPU,
    Modellverzeichnis und Phasenzeiten und einem kompletten `traceback` aus dem
    Worker. Der Traceback ist die brauchbarste Diagnose, die es zu einem
    gescheiterten Dokument gibt; die ganze Antwort steht deshalb als abfragbares
    JSON unter `raw["unverwertbare_antworten"]`.

    Dieselbe Form hatte schon die erste erzwungene Fehlerantwort unter MinerU 3.4
    (09.08.2026, `artifacts/2026-08-08-erster-tarball-lauf/`). Sie zeigte auch,
    dass der Worker die gueltigen Werte selbst nennt, wenn man ihn falsch fragt;
    das Paket prueft sie deshalb lokal, statt einen Kaltstart je Dokument dafuer
    zu bezahlen, unter MinerU 4 die Stufen (`config.GUELTIGE_STUFEN`)."""
    return ersatz.echte_antwort("fehlerantwort")


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


@pytest.fixture
def audio_dauerprobe(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Erfundene ffprobe-Antwort; Fristtests brauchen kein lokales Programm."""
    import subprocess

    probe = Mock(return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b'3600\n',
    ))
    monkeypatch.setattr('mineru_ocr.elevenlabs.shutil.which', lambda name: '/erfunden/ffprobe')
    monkeypatch.setattr('mineru_ocr.elevenlabs.subprocess.run', probe)
    return probe


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
def alte_documents_form(db_conn: Any) -> Iterator[None]:
    """Gibt `documents` die Form aus der Zeit, als das Abschicken die Zeile mit
    `new` anlegte — so, wie sie nach einem Paket-Update ohne `setup` noch
    dasteht —, und stellt die richtige Form danach wieder her."""
    db_conn.execute(
        "ALTER TABLE documents DROP CONSTRAINT documents_status_check, "
        "ADD CONSTRAINT documents_status_check CHECK "
        "(status IN ('new', 'done', 'empty', 'failed', 'input_error')), "
        "ALTER COLUMN status SET DEFAULT 'new'"
    )
    try:
        yield
    finally:
        db_conn.execute("DELETE FROM documents WHERE status = 'new'")
        db_conn.execute(
            "ALTER TABLE documents DROP CONSTRAINT documents_status_check, "
            "ADD CONSTRAINT documents_status_check CHECK "
            "(status IN ('done', 'empty', 'failed', 'input_error')), "
            "ALTER COLUMN status DROP DEFAULT"
        )


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
def lese_einstellungen_der_eingabe(db_conn: Any) -> Callable[[str], tuple[Any, ...]]:
    """Liest Stufe, OCR-Modus und Bildanalyse, die an der Eingabe eines Dokuments
    stehen (Mineroo#14): `(tier, ocr_mode, image_analysis)` je Hash."""

    def lesen(sha256: str) -> tuple[Any, ...]:
        zeile = db_conn.execute(
            "SELECT tier, ocr_mode, image_analysis FROM inputs WHERE sha256 = %s",
            (sha256,),
        ).fetchone()
        assert zeile is not None
        return tuple(zeile)

    return lesen


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
