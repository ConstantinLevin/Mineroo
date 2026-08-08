"""Die Tabelle: Schreibregeln, Wecksignal, abgeleiteter Zustand.

Die Tests zum abgeleiteten Zustand sind REIN und brauchen keine Datenbank — sie
bekommen Zeile und Job-Liste hereingereicht. Das ist der Grund, warum
`derive_status` eine reine Funktion ist: Die Regel ist der heikle Teil, die
Abfrage ist Handwerk.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from procrastinate.jobs import Job

from mineru_ocr.db import (
    DocumentRow,
    DocumentStatus,
    Phase,
    append_note_async,
    counts_by_status,
    derive_status,
    get_document,
    insert_document,
    list_documents,
    setup,
    wait_for_terminal,
    worker_heartbeats,
    write_terminal_async,
)
from mineru_ocr.errors import DocumentNotFound, TerminalOverwriteError

HASH = "a" * 64


def _zeile(status: DocumentStatus = DocumentStatus.NEW) -> DocumentRow:
    """Eine Zeile im Speicher, ohne Datenbank — fuer die reinen Tests."""
    jetzt = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    return DocumentRow(
        sha256=HASH,
        source_path="/tmp/x.pdf",
        source_name="x.pdf",
        size_bytes=1,
        mime="application/pdf",
        page_count=1,
        status=status,
        raw=None,
        error=None,
        endpoint_id="l79lebhinxaqjz",
        image_tag_at_submit=None,
        notes=[],
        created_at=jetzt,
        updated_at=jetzt,
    )


def _job(kennung: int, status: str, attempts: int = 0) -> Job:
    """Ein Job, wie Procrastinate ihn liefert — nur die Felder, die zaehlen."""
    return Job(
        id=kennung,
        status=status,
        queue="ocr",
        lock=HASH,
        queueing_lock=HASH,
        task_name="ocr_document",
        attempts=attempts,
    )


def _einfuegen(conn: psycopg.Connection[Any], sha: str = HASH) -> None:
    insert_document(
        conn,
        sha256=sha,
        source_path=f"/tmp/{sha}.pdf",
        source_name=f"{sha}.pdf",
        size_bytes=123,
        mime="application/pdf",
        page_count=3,
        endpoint_id="l79lebhinxaqjz",
    )


# --- rein, ohne Datenbank -------------------------------------------------


def test_terminale_zeile_schlaegt_jeden_job() -> None:
    """Ist die Zeile endgueltig, ist das der Zustand — auch wenn danach noch ein
    Job auftaucht. Ein fertiges Dokument bleibt fertig."""
    for status in [
        DocumentStatus.DONE,
        DocumentStatus.EMPTY,
        DocumentStatus.FAILED,
        DocumentStatus.INPUT_ERROR,
    ]:
        abgeleitet = derive_status(_zeile(status), [_job(7, "doing")])
        assert abgeleitet.phase is Phase.TERMINAL, status
        assert abgeleitet.row_status is status
        # Die Job-Angaben werden trotzdem mitgeliefert — sie helfen beim
        # Nachsehen, sie entscheiden nur nichts mehr.
        assert abgeleitet.job_id == 7
        assert abgeleitet.job_status == "doing"


def test_ohne_job_ist_das_dokument_nur_registriert() -> None:
    """Zeile `new`, keine Jobs: Phase REGISTERED."""
    abgeleitet = derive_status(_zeile(), [])
    assert abgeleitet.phase is Phase.REGISTERED
    assert abgeleitet.job_id is None
    assert abgeleitet.job_status is None
    assert abgeleitet.attempts is None
    assert abgeleitet.job_count == 0


def test_juengster_job_zaehlt_nicht_der_aelteste() -> None:
    """Bei mehreren Jobs entscheidet der mit der hoechsten Kennung.

    Job-Zeilen werden nie geloescht, also gibt es ueber die Zeit mehrere je
    Dokument — der aelteste sagt nichts ueber jetzt.
    """
    jobs = [_job(1, "failed"), _job(9, "doing"), _job(5, "succeeded")]
    abgeleitet = derive_status(_zeile(), jobs)
    assert abgeleitet.job_id == 9
    assert abgeleitet.phase is Phase.RUNNING
    assert abgeleitet.job_count == 3

    # Und die Reihenfolge der Liste darf nichts aendern — sortiert wird hier.
    umgekehrt = derive_status(_zeile(), list(reversed(jobs)))
    assert umgekehrt == abgeleitet


def test_wartender_job_ergibt_waiting() -> None:
    """Zeile `new`, juengster Job `todo`: Phase WAITING."""
    assert derive_status(_zeile(), [_job(1, "todo")]).phase is Phase.WAITING


def test_laufender_job_ergibt_running() -> None:
    """Zeile `new`, juengster Job `doing`: Phase RUNNING."""
    assert derive_status(_zeile(), [_job(1, "doing")]).phase is Phase.RUNNING


def test_beendeter_job_ohne_ergebnis_wird_erkannt() -> None:
    """Zeile `new`, juengster Job endgueltig: Phase ENDED_WITHOUT_RESULT.

    Der Fall nach hartem Abschuss oder Herunterfahren. Kein Datenfehler,
    sondern genau der Zustand, den das Ein-Buch-Modell erwartet — er heilt beim
    naechsten Einreihen von selbst.
    """
    for status in ["succeeded", "failed", "cancelled", "aborted"]:
        abgeleitet = derive_status(_zeile(), [_job(1, status)])
        assert abgeleitet.phase is Phase.ENDED_WITHOUT_RESULT, status
        assert abgeleitet.job_status == status


def test_unbekannter_job_zustand_gilt_als_wartend() -> None:
    """Ein Zustand, den Procrastinate spaeter einfuehrt, wird NICHT als
    abgeschlossen verbucht.

    Die bequeme Lesart waere ENDED_WITHOUT_RESULT — dann verschwaende ein
    Dokument aus dem Blick, weil es aussaehe, als sei es fertig behandelt.
    WAITING behaelt es im Blick.
    """
    assert derive_status(_zeile(), [_job(1, "irgendwas_neues")]).phase is Phase.WAITING


def test_versuchszaehler_wird_durchgereicht() -> None:
    """`attempts` des juengsten Jobs steht im abgeleiteten Zustand.

    Zur Reichweite: Das zaehlt JEDE Wiedervorlage — Strategie-Retry,
    stalled-Wiederaufnahme und Retry von Hand laufen durch dieselbe SQL-Funktion
    (Feature-Verifikation Punkt 19).
    """
    abgeleitet = derive_status(_zeile(), [_job(1, "todo", attempts=2)])
    assert abgeleitet.attempts == 2


# --- mit Datenbank --------------------------------------------------------


@pytest.mark.db
def test_setup_ist_mehrfach_aufrufbar(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Zweimal `setup` auf derselben Datenbank aendert nichts und wirft nicht.

    Das ist NICHT selbstverstaendlich, sondern der Grund fuer eine eigene
    Vorpruefung im Code: Procrastinates `apply_schema()` bricht beim zweiten
    Aufruf ab mit `DuplicateObject: type "procrastinate_job_status" already
    exists` — `CREATE TYPE` kennt in Postgres kein `IF NOT EXISTS`. Ohne die
    Vorpruefung waere die Zusicherung von `setup` schlicht falsch, und ein
    zweites `mineru-ocr setup` endete mit einem Absturz, dessen Meldung von
    einem Aufzaehlungstyp handelt, mit dem der Benutzer nie zu tun hatte.
    """
    _einfuegen(db_conn)
    setup(_test_datenbank)
    setup(_test_datenbank)
    # Und es zerstoert nichts: Die Zeile von vorhin steht noch da.
    assert get_document(db_conn, HASH) is not None


@pytest.mark.db
def test_setup_meldet_abweichendes_schema(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Passt die vorhandene Tabelle nicht zur Zeilenklasse, wirft `setup` mit
    Nennung der abweichenden Spalten und der Abhilfe — und veraendert NICHTS."""
    db_conn.execute("ALTER TABLE documents ADD COLUMN erfundene_spalte text")
    try:
        with pytest.raises(Exception) as fehler:
            setup(_test_datenbank)
        text = str(fehler.value)
        assert "erfundene_spalte" in text
        assert "setup" in text  # der Weg steht drin, nicht nur der Befund
        # Nichts angefasst: Die Spalte ist noch da, die Tabelle steht.
        assert db_conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='documents' AND column_name='erfundene_spalte'"
        ).fetchone() is not None
    finally:
        db_conn.execute("ALTER TABLE documents DROP COLUMN erfundene_spalte")


@pytest.mark.db
def test_zweites_einfuegen_ist_ein_nichts_tun(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Derselbe Hash ein zweites Mal ergibt False und laesst die bestehende
    Zeile voellig unberuehrt — auch `updated_at`.

    Darauf beruht das Ordner-Szenario: „lässt nochmal durchlaufen weil eh nur
    läuft was noch fehlt".
    """
    assert insert_document(
        db_conn,
        sha256=HASH,
        source_path="/tmp/erste.pdf",
        source_name="erste.pdf",
        size_bytes=111,
        mime="application/pdf",
        page_count=1,
        endpoint_id="e1",
    )
    vorher = get_document(db_conn, HASH)
    assert vorher is not None

    assert not insert_document(
        db_conn,
        sha256=HASH,
        source_path="/tmp/zweite.pdf",
        source_name="zweite.pdf",
        size_bytes=999,
        mime="image/png",
        page_count=None,
        endpoint_id="e2",
    )
    nachher = get_document(db_conn, HASH)
    assert nachher == vorher


@pytest.mark.db
async def test_terminal_schreiben_setzt_alles_in_einer_transaktion(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Nach dem Aufruf sind Zustand, raw, error, notes und updated_at gesetzt —
    es gibt keinen Zwischenzustand mit raw und Status `new`."""
    _einfuegen(db_conn)
    vorher = get_document(db_conn, HASH)
    assert vorher is not None

    await write_terminal_async(
        async_conn,
        sha256=HASH,
        status=DocumentStatus.DONE,
        raw={"markdown": "Text", "content_list": []},
        image_tag_at_submit=":17d4955b2",
        notes=[{"kind": "hinweis", "at": "2026-08-08"}],
    )

    nachher = get_document(db_conn, HASH)
    assert nachher is not None
    assert nachher.status is DocumentStatus.DONE
    assert nachher.raw == {"markdown": "Text", "content_list": []}
    assert nachher.image_tag_at_submit == ":17d4955b2"
    assert nachher.notes == [{"kind": "hinweis", "at": "2026-08-08"}]
    assert nachher.updated_at > vorher.updated_at


@pytest.mark.db
async def test_neuer_zustand_wird_abgewiesen(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """`write_terminal_async` mit `new` ist ein Programmierfehler.

    Die Funktion heisst „terminal" — sie mit einem nicht endgueltigen Zustand zu
    rufen hiesse, die Zeile in einen Zustand zu schreiben, aus dem sie ohnehin
    kommt, und dabei `raw` zu setzen. Genau der Zwischenzustand, den es nicht
    geben soll.
    """
    _einfuegen(db_conn)
    with pytest.raises(ValueError):
        await write_terminal_async(
            async_conn, sha256=HASH, status=DocumentStatus.NEW, raw={}
        )


@pytest.mark.db
async def test_schreiben_ohne_zeile_wirft(
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """Ein Endergebnis ohne Zeile gibt es nicht."""
    with pytest.raises(DocumentNotFound):
        await write_terminal_async(
            async_conn, sha256="f" * 64, status=DocumentStatus.DONE, raw={}
        )


@pytest.mark.db
async def test_wecksignal_kommt_bei_jedem_endzustand(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auch `failed` und `input_error` loesen das Signal aus.

    Weckte es nur bei Erfolg, liefe jeder Warter auf ein gescheitertes Dokument
    bis zum Zeitablauf und wuesste am Ende nicht, ob es scheiterte oder dauert.
    """
    for nr, status in enumerate(
        [
            DocumentStatus.DONE,
            DocumentStatus.EMPTY,
            DocumentStatus.FAILED,
            DocumentStatus.INPUT_ERROR,
        ]
    ):
        sha = f"{nr:064d}"
        _einfuegen(db_conn, sha)

        db_conn.execute("LISTEN doc_done")
        await write_terminal_async(
            async_conn, sha256=sha, status=status, raw={"x": nr}
        )

        signale = list(db_conn.notifies(timeout=5.0, stop_after=1))
        assert signale, f"kein Signal bei {status.value}"
        assert signale[0].payload == sha, status.value
        db_conn.execute("UNLISTEN doc_done")


@pytest.mark.db
async def test_fertiges_dokument_wird_nie_ueberschrieben(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auf `done`, `empty` oder `input_error` wirft ein abweichender
    Schreibvorgang TerminalOverwriteError."""
    for nr, status in enumerate(
        [DocumentStatus.DONE, DocumentStatus.EMPTY, DocumentStatus.INPUT_ERROR]
    ):
        sha = f"{nr:064d}"
        _einfuegen(db_conn, sha)
        await write_terminal_async(
            async_conn, sha256=sha, status=status, raw={"erste": True}
        )
        with pytest.raises(TerminalOverwriteError):
            await write_terminal_async(
                async_conn,
                sha256=sha,
                status=DocumentStatus.FAILED,
                raw={"zweite": True},
            )
        # Und das erste Ergebnis steht unveraendert.
        zeile = get_document(db_conn, sha)
        assert zeile is not None
        assert zeile.raw == {"erste": True}


@pytest.mark.db
async def test_gescheitertes_dokument_darf_ueberschrieben_werden(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auf `failed` schreibt derselbe Aufruf durch — das ist der Wiederanlauf
    (Plan 4.2) und der Grund, warum der Terminal-Guard `failed` nicht deckt.

    Stuende `failed` im Guard, waere jeder Retry von Hand ein Nichts-Tun, das als
    Erfolg verbucht wuerde, und ein gescheitertes Dokument koennte nie wieder
    laufen.
    """
    _einfuegen(db_conn)
    await write_terminal_async(
        async_conn,
        sha256=HASH,
        status=DocumentStatus.FAILED,
        raw={"versuch": 1},
        error="Zeitablauf",
    )
    await write_terminal_async(
        async_conn, sha256=HASH, status=DocumentStatus.DONE, raw={"versuch": 2}
    )
    zeile = get_document(db_conn, HASH)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw == {"versuch": 2}


@pytest.mark.db
async def test_gleicher_aufruf_zweimal_ist_unschaedlich(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Zweimal derselbe Endzustand wirft nicht — und der ERSTE Schreibvorgang
    gewinnt.

    Der Massstab ist der Zustand, nicht der Inhalt: `raw` traegt Job-Kennungen
    und Laufzeiten, ist also bei zwei Laeufen nie gleich. Ein Inhaltsvergleich
    waere immer „abweichend" und machte den harmlosen Wiederlauf nach einem
    Absturz im Schreibfenster zum Fehler.
    """
    _einfuegen(db_conn)
    await write_terminal_async(
        async_conn, sha256=HASH, status=DocumentStatus.DONE, raw={"lauf": 1}
    )
    await write_terminal_async(
        async_conn, sha256=HASH, status=DocumentStatus.DONE, raw={"lauf": 2}
    )
    zeile = get_document(db_conn, HASH)
    assert zeile is not None
    assert zeile.raw == {"lauf": 1}


@pytest.mark.db
async def test_notiz_haengt_an_statt_zu_ersetzen(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """`append_note_async` verlaengert die Liste und beruehrt sonst nichts."""
    _einfuegen(db_conn)
    vorher = get_document(db_conn, HASH)
    assert vorher is not None

    await append_note_async(async_conn, HASH, {"kind": "erste", "at": "t1"})
    await append_note_async(async_conn, HASH, {"kind": "zweite", "at": "t2"})

    nachher = get_document(db_conn, HASH)
    assert nachher is not None
    assert nachher.notes == [
        {"kind": "erste", "at": "t1"},
        {"kind": "zweite", "at": "t2"},
    ]
    # Sonst nichts: `notes` ist eine Beobachtung, kein Zustand.
    assert nachher.status is vorher.status
    assert nachher.updated_at == vorher.updated_at


@pytest.mark.db
async def test_notiz_ohne_zeile_wirft(
    async_conn: psycopg.AsyncConnection[Any],
) -> None:
    """Eine Beobachtung, die ins Leere geschrieben wird, faellt sonst nie auf."""
    with pytest.raises(DocumentNotFound):
        await append_note_async(async_conn, "e" * 64, {"kind": "x"})


@pytest.mark.db
async def test_warten_findet_bereits_fertiges_dokument_sofort(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Ist das Dokument schon endgueltig, kehrt `wait_for_terminal` sofort
    zurueck, statt auf ein Signal zu warten, das schon vorbei ist."""
    _einfuegen(db_conn)
    await write_terminal_async(
        async_conn, sha256=HASH, status=DocumentStatus.DONE, raw={"fertig": True}
    )
    begonnen = time.monotonic()
    zeile = wait_for_terminal(db_conn, HASH, timeout=30.0)
    assert time.monotonic() - begonnen < 1.0
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw == {"fertig": True}


@pytest.mark.db
def test_warten_auf_unbekannten_hash_wirft_sofort(
    db_conn: psycopg.Connection[Any],
) -> None:
    """DocumentNotFound kommt sofort, nicht nach Ablauf der Wartezeit."""
    begonnen = time.monotonic()
    with pytest.raises(DocumentNotFound):
        wait_for_terminal(db_conn, "d" * 64, timeout=30.0)
    assert time.monotonic() - begonnen < 1.0


@pytest.mark.db
def test_warten_laeuft_ab(db_conn: psycopg.Connection[Any]) -> None:
    """Bleibt das Dokument `new`, endet das Warten mit TimeoutError.

    Und die Meldung sagt, was der Zeitablauf bedeutet: etwas ueber die Geduld
    des Wartenden, nichts ueber das Dokument.
    """
    _einfuegen(db_conn)
    with pytest.raises(TimeoutError) as fehler:
        wait_for_terminal(db_conn, HASH, timeout=1.0, poll_seconds=0.3)
    assert "Geduld" in str(fehler.value)


@pytest.mark.db
def test_warten_faellt_auf_abfragen_zurueck(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Auch ohne Signal — etwa bei einem Schreibvorgang von aussen — endet das
    Warten durch die Rueckfallabfrage.

    Geprueft wird das, indem der Zustand mit ROHEM SQL gesetzt wird, ganz ohne
    `pg_notify`. Genau so sieht ein Schreibvorgang von Hand an der Datenbank aus,
    und genau dagegen ist die Rueckfallabfrage da.
    """
    _einfuegen(db_conn)

    def spaeter_setzen() -> None:
        time.sleep(0.5)
        with psycopg.connect(_test_datenbank, autocommit=True) as andere:
            andere.execute(
                "UPDATE documents SET status = 'done' WHERE sha256 = %s", (HASH,)
            )

    faden = threading.Thread(target=spaeter_setzen)
    faden.start()
    try:
        zeile = wait_for_terminal(db_conn, HASH, timeout=10.0, poll_seconds=0.2)
    finally:
        faden.join()
    assert zeile.status is DocumentStatus.DONE


@pytest.mark.db
async def test_liste_laedt_kein_raw(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """`list_documents` liefert Zeilen mit `raw is None`, obwohl in der
    Datenbank etwas steht. Bei dreissigtausend Dokumenten waeren es sonst
    zweistellige Gigabyte.

    Und `raw_loaded` sagt, dass es NICHT geladen wurde — ohne diese
    Unterscheidung liefert eine Huelle aus einer gelisteten Zeile fuer ein
    fertiges Dokument stillschweigend nichts, und das sieht aus wie ein leeres
    Ergebnis.
    """
    _einfuegen(db_conn)
    await write_terminal_async(
        async_conn, sha256=HASH, status=DocumentStatus.DONE, raw={"gross": "x" * 1000}
    )

    gelistet = list_documents(db_conn)
    assert len(gelistet) == 1
    assert gelistet[0].raw is None
    assert gelistet[0].raw_loaded is False

    # Das Einzel-Lesen laedt es sehr wohl, und sagt das auch.
    einzeln = get_document(db_conn, HASH)
    assert einzeln is not None
    assert einzeln.raw == {"gross": "x" * 1000}
    assert einzeln.raw_loaded is True


@pytest.mark.db
async def test_liste_filtert_nach_zustand(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Der Zustandsfilter waehlt aus, ohne dass der Aufrufer selbst sieben muss."""
    for nr in range(3):
        _einfuegen(db_conn, f"{nr:064d}")
    await write_terminal_async(
        async_conn, sha256=f"{0:064d}", status=DocumentStatus.DONE, raw={}
    )

    assert len(list_documents(db_conn, status=DocumentStatus.DONE)) == 1
    assert len(list_documents(db_conn, status=DocumentStatus.NEW)) == 2
    assert len(list_documents(db_conn)) == 3
    assert len(list_documents(db_conn, limit=2)) == 2


@pytest.mark.db
def test_zaehlung_kennt_jeden_zustand(db_conn: psycopg.Connection[Any]) -> None:
    """Auch Zustaende ohne Zeilen kommen mit Null vor — ein fehlender
    Schluessel wuerde die Statusausgabe genau dann sprengen, wenn alles gut
    lief."""
    zaehlung = counts_by_status(db_conn)
    assert set(zaehlung) == set(DocumentStatus)
    assert all(anzahl == 0 for anzahl in zaehlung.values())

    _einfuegen(db_conn)
    assert counts_by_status(db_conn)[DocumentStatus.NEW] == 1


@pytest.mark.db
def test_worker_lebenszeichen_sind_lesbar(db_conn: psycopg.Connection[Any]) -> None:
    """Die undokumentierte Flaeche antwortet — und eine leere Liste ist eine
    Feststellung, kein Fehler.

    Der Aufbau der Tabelle ist am Objekt der installierten Fassung festgestellt
    (`procrastinate_workers` mit `id bigint` und `last_heartbeat timestamptz`);
    die Feature-Verifikation hatte an dieser Stelle nur eine Vorhersage. Bricht
    dieser Test nach einem Procrastinate-Upgrade, ist genau das die Meldung, die
    man dann lesen will.
    """
    assert worker_heartbeats(db_conn) == []

    db_conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES")
    lebende = worker_heartbeats(db_conn)
    assert len(lebende) == 1
    kennung, gesehen = lebende[0]
    assert isinstance(kennung, int)
    assert isinstance(gesehen, datetime)
