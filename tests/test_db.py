"""Die Tabellen: Schreibregeln, Wecksignal, Stand und abgeleiteter Zustand.

Die Tests zum abgeleiteten Zustand sind REIN und brauchen keine Datenbank — sie
bekommen Ergebnis und Job-Liste hereingereicht. Das ist der Grund, warum
`derive_status` eine reine Funktion ist: Die Regel ist der heikle Teil, die
Abfrage ist Handwerk.
"""

from __future__ import annotations

import asyncio
import inspect
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import procrastinate.worker
import psycopg
import psycopg.sql
import pytest
from procrastinate.jobs import Job

from mineru_ocr.config import DAEMON_HEARTBEAT_MAX_AGE_SECONDS
from mineru_ocr.db import (
    WAIT_APPLICATION_NAME,
    DocumentRow,
    DocumentState,
    DocumentStatus,
    Phase,
    check_schema,
    count_open,
    counts_by_status,
    delete_input_async,
    derive_status,
    get_document,
    get_status_async,
    insert_input,
    list_documents,
    read_input_async,
    read_states,
    setup,
    wait_for_terminal,
    wait_for_terminal_async,
    worker_heartbeats,
    write_terminal_async,
)
from mineru_ocr.errors import (
    ConfigurationError,
    DatabaseError,
    DocumentNotFound,
    TerminalOverwriteError,
    WaitTimeoutError,
)

HASH = "a" * 64


def _zeile(status: DocumentStatus = DocumentStatus.DONE) -> DocumentRow:
    """Eine Ergebniszeile im Speicher, ohne Datenbank — fuer die reinen Tests."""
    jetzt = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    return DocumentRow(
        sha256=HASH,
        size_bytes=1,
        mime="application/pdf",
        page_count=1,
        status=status,
        raw=None,
        error=None,
        endpoint_id="testendpoint",
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
        queueing_lock=None,
        task_name="ocr_document",
        attempts=attempts,
    )


async def _schreiben(
    conn: psycopg.AsyncConnection[Any],
    sha: str = HASH,
    status: DocumentStatus = DocumentStatus.DONE,
    raw: dict[str, Any] | None = None,
    **weiter: Any,
) -> None:
    """Das Endschreiben des Jobs, mit den Angaben, die jeder Lauf mitgibt."""
    await write_terminal_async(
        conn,
        sha256=sha,
        status=status,
        raw=raw if raw is not None else {},
        endpoint_id=weiter.pop("endpoint_id", "testendpoint"),
        **weiter,
    )


def _ergebnis_anlegen(
    conn: psycopg.Connection[Any],
    sha: str = HASH,
    status: DocumentStatus = DocumentStatus.DONE,
) -> None:
    """Eine Ergebniszeile von Hand, fuer die synchronen Tests ohne Job."""
    conn.execute(
        "INSERT INTO documents (sha256, status, endpoint_id) VALUES (%s, %s, %s)",
        (sha, status.value, "testendpoint"),
    )


def _eingabe(conn: psycopg.Connection[Any], sha: str = HASH) -> bool:
    """Ob zu `sha` eine Eingabe steht."""
    return (
        conn.execute("SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)).fetchone()
        is not None
    )


# --- rein, ohne Datenbank -------------------------------------------------


def test_ergebnis_schlaegt_jeden_job() -> None:
    """Gibt es ein Ergebnis, ist das der Zustand — auch wenn danach noch ein Job
    auftaucht. Ein fertiges Dokument bleibt fertig."""
    for status in DocumentStatus:
        abgeleitet = derive_status(HASH, _zeile(status), _job(7, "doing"))
        assert abgeleitet.phase is Phase.TERMINAL, status
        assert abgeleitet.row_status is status
        # Die Job-Angaben werden trotzdem mitgeliefert — sie helfen beim
        # Nachsehen, sie entscheiden nur nichts mehr.
        assert abgeleitet.job_id == 7
        assert abgeleitet.job_status == "doing"


def test_documents_kennt_nur_endzustaende() -> None:
    """`new` gibt es nicht mehr: Die Zeile legt der Job an, wenn er endet."""
    assert {zustand.value for zustand in DocumentStatus} == {
        "done",
        "empty",
        "failed",
        "input_error",
    }
    assert "REGISTERED" not in Phase.__members__


def test_offen_ohne_job_ist_ohne_ergebnis_beendet() -> None:
    """Eingabe da, kein Ergebnis, kein Job: Unterwegs ist nichts mehr, `retry`
    hilft. Eingabe und Job entstehen in einer Transaktion, es gab also einen."""
    abgeleitet = derive_status(HASH, None, None)
    assert abgeleitet.phase is Phase.ENDED_WITHOUT_RESULT
    assert abgeleitet.row_status is None
    assert abgeleitet.job_id is None
    assert abgeleitet.job_status is None
    assert abgeleitet.attempts is None


def test_wartender_job_ergibt_waiting() -> None:
    """Offen, sein Job `todo`: Phase WAITING."""
    assert derive_status(HASH, None, _job(1, "todo")).phase is Phase.WAITING


def test_laufender_job_ergibt_running() -> None:
    """Offen, sein Job `doing`: Phase RUNNING."""
    assert derive_status(HASH, None, _job(1, "doing")).phase is Phase.RUNNING


def test_beendeter_job_ohne_ergebnis_wird_erkannt() -> None:
    """Offen, sein Job endgueltig: Phase ENDED_WITHOUT_RESULT.

    Der Fall nach hartem Abschuss oder Herunterfahren. Kein Datenfehler,
    sondern genau der Zustand, den das Ein-Buch-Modell erwartet — erneutes
    Abschicken heilt ihn nicht (die Eingabe steht, das Dokument gilt als
    unterwegs), den Rueckweg bringt `retry`.
    """
    for status in ["succeeded", "failed", "cancelled", "aborted"]:
        abgeleitet = derive_status(HASH, None, _job(1, status))
        assert abgeleitet.phase is Phase.ENDED_WITHOUT_RESULT, status
        assert abgeleitet.job_status == status


def test_unbekannter_job_zustand_gilt_als_wartend() -> None:
    """Ein Zustand, den Procrastinate spaeter einfuehrt, wird NICHT als
    abgeschlossen verbucht.

    Die bequeme Lesart waere ENDED_WITHOUT_RESULT — dann verschwaende ein
    Dokument aus dem Blick, weil es aussaehe, als sei es fertig behandelt.
    WAITING behaelt es im Blick.
    """
    assert derive_status(HASH, None, _job(1, "irgendwas_neues")).phase is Phase.WAITING


def test_versuchszaehler_wird_durchgereicht() -> None:
    """`attempts` seines Jobs steht im abgeleiteten Zustand.

    Zur Reichweite: Das zaehlt JEDE Wiedervorlage — Strategie-Retry,
    stalled-Wiederaufnahme und Retry von Hand laufen durch dieselbe SQL-Funktion
    (Feature-Verifikation Punkt 19).
    """
    abgeleitet = derive_status(HASH, None, _job(1, "todo", attempts=2))
    assert abgeleitet.attempts == 2


def test_stand_als_abbildung_ohne_raw() -> None:
    """`to_dict` ist die Ausgabe von `show --json`: bei einem Ergebnis dessen
    Angaben, bei einem offenen Dokument nur Hash, Phase und Job — nie `raw`."""
    fertig = derive_status(HASH, _zeile(DocumentStatus.DONE), _job(3, "succeeded")).to_dict()
    assert fertig["status"] == "done"
    assert fertig["phase"] == "terminal"
    assert fertig["mime"] == "application/pdf"
    assert fertig["job"] == {"id": 3, "status": "succeeded", "attempts": 0}
    assert "raw" not in fertig

    offen = derive_status(HASH, None, _job(4, "todo")).to_dict()
    assert offen == {
        "sha256": HASH,
        "status": None,
        "phase": "waiting",
        "job": {"id": 4, "status": "todo", "attempts": 0},
    }


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
    _ergebnis_anlegen(db_conn)
    insert_input(db_conn, sha256="b" * 64, content=b"offen")
    setup(_test_datenbank)
    setup(_test_datenbank)
    # Und es zerstoert nichts: Ergebnis und Eingabe von vorhin stehen noch da.
    assert get_document(db_conn, HASH) is not None
    assert _eingabe(db_conn, "b" * 64)


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
def test_setup_meldet_ein_documents_aus_der_zeit_mit_new(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Eine Tabelle, die noch `new` kennt, hat dieselben Spalten — und passt doch
    nicht: Der Job legt die Zeile erst am Ende an. `setup` sagt das samt Abhilfe,
    statt dass der erste Job daran scheitert."""
    db_conn.execute(
        "ALTER TABLE documents DROP CONSTRAINT documents_status_check, "
        "ADD CONSTRAINT documents_status_check CHECK "
        "(status IN ('new', 'done', 'empty', 'failed', 'input_error')), "
        "ALTER COLUMN status SET DEFAULT 'new'"
    )
    try:
        with pytest.raises(DatabaseError) as fehler:
            setup(_test_datenbank)
        text = str(fehler.value)
        assert "documents" in text and "'new'" in text and "setup" in text
    finally:
        db_conn.execute(
            "ALTER TABLE documents DROP CONSTRAINT documents_status_check, "
            "ADD CONSTRAINT documents_status_check CHECK "
            "(status IN ('done', 'empty', 'failed', 'input_error')), "
            "ALTER COLUMN status DROP DEFAULT"
        )

    # Einzeln: auch ein `mime` mit NOT NULL ist die alte Form.
    db_conn.execute("ALTER TABLE documents ALTER COLUMN mime SET NOT NULL")
    try:
        with pytest.raises(DatabaseError, match="mime"):
            setup(_test_datenbank)
    finally:
        db_conn.execute("ALTER TABLE documents ALTER COLUMN mime DROP NOT NULL")
    setup(_test_datenbank)  # wieder in Ordnung


@pytest.mark.db
def test_check_schema_laesst_die_richtige_form_durch(
    db_conn: psycopg.Connection[Any],
) -> None:
    check_schema(db_conn)


@pytest.mark.db
def test_check_schema_weist_die_alte_form_ab_und_aendert_nichts(
    db_conn: psycopg.Connection[Any], alte_documents_form: None
) -> None:
    """Nach einem Paket-Update ohne `setup` steht die alte Tabelle noch da, mit
    `new`-Zeilen. `check_schema` sagt das samt Abhilfe, statt dass der erste
    Lesezugriff an einem `ValueError` scheitert — und rein lesend."""
    db_conn.execute(
        "INSERT INTO documents (sha256, endpoint_id, mime, size_bytes) "
        "VALUES (%s, 'e', 'image/png', 1)",
        (HASH,),
    )
    with pytest.raises(DatabaseError) as fehler:
        check_schema(db_conn)
    text = str(fehler.value)
    assert "'new'" in text and "setup" in text
    zeile = db_conn.execute(
        "SELECT status FROM documents WHERE sha256 = %s", (HASH,)
    ).fetchone()
    assert zeile == ("new",)


@pytest.mark.db
@pytest.mark.parametrize(
    "aenderung",
    [
        "ALTER TABLE inputs ALTER COLUMN job_id SET NOT NULL",
        "ALTER TABLE inputs ALTER COLUMN job_id TYPE text",
    ],
)
def test_check_schema_weist_eine_falsche_form_von_job_id_ab(
    db_conn: psycopg.Connection[Any], aenderung: str
) -> None:
    """`job_id` muss `bigint` sein und leer sein duerfen: Das Abschicken legt die
    Eingabe ohne Kennung an und schreibt sie erst danach. Eine Tabelle mit
    `job_id ... NOT NULL` liesse sonst jedes Abschicken scheitern, obwohl die
    Pruefung sie durchgelassen haette."""
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(aenderung)
        with pytest.raises(DatabaseError, match="job_id"):
            check_schema(db_conn)


@pytest.mark.db
def test_check_schema_nennt_ein_fehlendes_schema_und_setup(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Fehlen die Tabellen ganz (nie `setup`), ist das eine Meldung mit dem Weg,
    kein `UndefinedTable` aus der Tiefe."""
    db_conn.execute("CREATE SCHEMA IF NOT EXISTS leer_fuer_test")
    try:
        db_conn.execute("SET search_path TO leer_fuer_test")
        with pytest.raises(DatabaseError, match="setup"):
            check_schema(db_conn)
    finally:
        db_conn.execute("SET search_path TO DEFAULT")
        db_conn.execute("DROP SCHEMA leer_fuer_test")


@pytest.mark.db
def test_documents_kennt_keinen_zustand_new(db_conn: psycopg.Connection[Any]) -> None:
    """Die Datenbank selbst weist `new` ab, und `status` hat keine
    Voreinstellung, mit der eine Zeile ohne Ergebnis entstehen koennte."""
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO documents (sha256, status, endpoint_id) "
            "VALUES (%s, 'new', 'e')",
            (HASH,),
        )
    with pytest.raises(psycopg.errors.NotNullViolation):
        db_conn.execute(
            "INSERT INTO documents (sha256, endpoint_id) VALUES (%s, 'e')", (HASH,)
        )


@pytest.mark.db
def test_documents_kennt_keine_pfade(db_conn: psycopg.Connection[Any]) -> None:
    """Pfad und Name stehen nicht in der Datenbank — weder als Spalte noch als
    Index."""
    spalten = {
        zeile[0]
        for zeile in db_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'documents'"
        ).fetchall()
    }
    assert not {"source_path", "source_name"} & spalten
    indizes = [
        zeile[0]
        for zeile in db_conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'documents'"
        ).fetchall()
    ]
    assert not [name for name in indizes if "source" in name]


@pytest.mark.db
def test_setup_legt_inputs_an(db_conn: psycopg.Connection[Any]) -> None:
    """Die Eingabe hat eine eigene Tabelle: `sha256` als Schluessel, `content` als
    `bytea` — nicht base64 in einer Textspalte —, und `job_id`, die Kennung des
    Jobs, ueber die `document()` und `retry` ihn finden."""
    spalten = dict(
        db_conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'inputs'"
        ).fetchall()
    )
    assert spalten == {"sha256": "text", "content": "bytea", "job_id": "bigint"}
    schluessel = db_conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'inputs'::regclass AND i.indisprimary"
    ).fetchall()
    assert [zeile[0] for zeile in schluessel] == ["sha256"]


@pytest.mark.db
def test_setup_meldet_abweichende_inputs_tabelle(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Der Abgleich kennt auch `inputs`: Eine Spalte zu viel wird mit Tabelle,
    Spalte und Abhilfe gemeldet, und nichts wird veraendert."""
    db_conn.execute("ALTER TABLE inputs ADD COLUMN erfundene_spalte text")
    try:
        with pytest.raises(Exception) as fehler:
            setup(_test_datenbank)
        text = str(fehler.value)
        assert "inputs" in text
        assert "erfundene_spalte" in text
        assert "setup" in text
        assert db_conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='inputs' AND column_name='erfundene_spalte'"
        ).fetchone() is not None
    finally:
        db_conn.execute("ALTER TABLE inputs DROP COLUMN erfundene_spalte")


@pytest.mark.db
def test_setup_meldet_eine_inputs_tabelle_ohne_primaerschluessel(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Richtige Spaltennamen genuegen nicht: Ohne Primaerschluessel auf `sha256`
    scheitert `ON CONFLICT (sha256)` beim ersten Abschicken, und `setup` haette
    vorher Erfolg gemeldet."""
    db_conn.execute("ALTER TABLE inputs DROP CONSTRAINT inputs_pkey")
    try:
        with pytest.raises(Exception) as fehler:
            setup(_test_datenbank)
        text = str(fehler.value)
        assert "inputs" in text
        assert "Primaerschluessel" in text
        assert "setup" in text
    finally:
        db_conn.execute("ALTER TABLE inputs ADD PRIMARY KEY (sha256)")


@pytest.mark.db
def test_setup_meldet_eine_inputs_tabelle_mit_falschem_spaltentyp(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """`content text` statt `bytea` passt in den Namen und nicht in die Form."""
    db_conn.execute("ALTER TABLE inputs ALTER COLUMN content TYPE text USING ''")
    try:
        with pytest.raises(Exception) as fehler:
            setup(_test_datenbank)
        assert "bytea" in str(fehler.value)
    finally:
        db_conn.execute(
            "ALTER TABLE inputs ALTER COLUMN content TYPE bytea USING ''::bytea"
        )


@pytest.mark.db
def test_setup_meldet_eine_inputs_tabelle_mit_nullbarem_content_oder_falschem_hash_typ(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Jede der Formbedingungen schlaegt einzeln an: `content` ohne NOT NULL und
    `sha256` als anderer Typ als `text`."""
    db_conn.execute("ALTER TABLE inputs ALTER COLUMN content DROP NOT NULL")
    try:
        with pytest.raises(Exception, match="NOT NULL"):
            setup(_test_datenbank)
    finally:
        db_conn.execute("ALTER TABLE inputs ALTER COLUMN content SET NOT NULL")

    db_conn.execute("ALTER TABLE inputs ALTER COLUMN sha256 TYPE varchar(64)")
    try:
        with pytest.raises(Exception, match="PRIMARY KEY"):
            setup(_test_datenbank)
    finally:
        db_conn.execute("ALTER TABLE inputs ALTER COLUMN sha256 TYPE text")


@pytest.mark.db
def test_setup_laesst_sich_nicht_von_einer_gleichnamigen_tabelle_anderswo_irren(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Eine Tabelle `inputs` in einem anderen Schema gehoert nicht zur Pruefung:
    Der Abgleich liest die Tabelle, die `search_path` aufloest."""
    db_conn.execute("CREATE SCHEMA fremd")
    try:
        # Mit einer ueberzaehligen Spalte und anderem Typ: Mischte der Abgleich
        # die Tabellen aller Schemas, schluege er hier sicher an.
        db_conn.execute(
            "CREATE TABLE fremd.inputs (sha256 text, content text, zusatz text)"
        )
        setup(_test_datenbank)  # wirft nicht
    finally:
        db_conn.execute("DROP SCHEMA fremd CASCADE")


@pytest.mark.db
async def test_eingabe_wird_gespeichert_und_binaer_gelesen(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Die Bytes gehen verlustfrei hinein und als `bytes` wieder heraus.

    Das Ergebnis muss `bytes` sein und nichts anderes: pypdfium2 nimmt ein
    `memoryview` nicht an. Alle 256 Bytewerte, damit eine Kodierung (hex,
    base64) an einem Wert auffaellt.
    """
    inhalt = bytes(range(256)) * 40
    assert insert_input(db_conn, sha256=HASH, content=inhalt) is True

    gelesen = await read_input_async(_test_datenbank, HASH)
    assert type(gelesen) is bytes
    assert gelesen == inhalt


@pytest.mark.db
def test_zweite_eingabe_zum_selben_hash_ist_ein_nichts_tun(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Derselbe Hash ein zweites Mal ergibt False und aendert nichts. Die
    Eingabe-Tabelle ist so die Sperre gegen ein zweites Einreihen."""
    assert insert_input(db_conn, sha256=HASH, content=b"erste") is True
    assert insert_input(db_conn, sha256=HASH, content=b"zweite") is False
    zeile = db_conn.execute(
        "SELECT content FROM inputs WHERE sha256 = %s", (HASH,)
    ).fetchone()
    assert zeile is not None
    assert bytes(zeile[0]) == b"erste"
    # Und eine Ergebniszeile legt das Ablegen nicht an.
    assert get_document(db_conn, HASH) is None


@pytest.mark.db
async def test_fehlende_eingabe_liest_sich_als_none(_test_datenbank: str) -> None:
    """Gibt es keine Eingabe zum Hash, ist die Antwort `None` — der Aufrufer
    entscheidet, was das heisst."""
    assert await read_input_async(_test_datenbank, "f" * 64) is None


class _Beobachter:
    """Eine Verbindung, die mitschreibt, WIE gelesen und OB geschlossen wurde.

    Am Ergebnis allein sieht man beides nicht: `bytes` kommen auch im Textformat
    heraus (nur doppelt so gross ueber die Leitung), und eine nicht geschlossene
    Verbindung faellt erst im Gigabyte auf. Gezaehlt wird nicht an
    `pg_stat_activity`, denn ein Backend lebt nach dem Schliessen noch einen
    Augenblick.
    """

    def __init__(self, echte: psycopg.AsyncConnection[Any], *, fehler: bool) -> None:
        self._echte = echte
        self._fehler = fehler
        self.binary: object = "nie gefragt"
        self.geschlossen = False

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.binary = kwargs.get("binary")
        if self._fehler:
            raise psycopg.OperationalError("Verbindung weg")
        return await self._echte.execute(*args, **kwargs)

    async def close(self) -> None:
        self.geschlossen = True
        await self._echte.close()


def _beobachten(monkeypatch: pytest.MonkeyPatch, *, fehler: bool) -> list[_Beobachter]:
    """Ersetzt `connect_async` im Modul durch eine, die jede Verbindung beobachtet."""
    from mineru_ocr import db

    echtes_verbinden = db.connect_async
    beobachter: list[_Beobachter] = []

    async def verbinden(dsn: str) -> Any:
        b = _Beobachter(await echtes_verbinden(dsn), fehler=fehler)
        beobachter.append(b)
        return b

    monkeypatch.setattr(db, "connect_async", verbinden)
    return beobachter


@pytest.mark.db
async def test_lesen_der_eingabe_ist_binaer_und_schliesst_die_eigene_verbindung(
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Der Job liest mit `binary=True` auf einer eigenen Verbindung, die danach zu ist.

    libpq behaelt seine Puffer in der Groesse des groessten je gelesenen Werts,
    bis die Verbindung schliesst; ohne `binary=True` ginge der Wert als Hex ueber
    die Leitung, doppelt so gross.
    """
    insert_input(db_conn, sha256=HASH, content=b"x" * 1_000_000)
    beobachter = _beobachten(monkeypatch, fehler=False)

    await read_input_async(_test_datenbank, HASH)

    assert len(beobachter) == 1
    assert beobachter[0].binary is True
    assert beobachter[0].geschlossen


@pytest.mark.db
async def test_lesefehler_der_eingabe_wird_uebersetzt_und_schliesst_trotzdem(
    _test_datenbank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein Fehler der Instanz beim Lesen wird `DatabaseError` (wiederholbar), und die
    Verbindung ist auch dann zu."""
    from mineru_ocr.errors import DatabaseError

    beobachter = _beobachten(monkeypatch, fehler=True)

    with pytest.raises(DatabaseError, match="Eingabe lesen"):
        await read_input_async(_test_datenbank, HASH)

    assert beobachter[0].geschlossen


@pytest.mark.db
async def test_eingabe_wird_mit_dem_endergebnis_geloescht_ausser_bei_failed(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Nach `done`, `empty` und `input_error` ist die Eingabe weg; nach `failed`
    steht sie noch, damit `retry` etwas zu lesen hat."""
    erwartet = {
        DocumentStatus.DONE: False,
        DocumentStatus.EMPTY: False,
        DocumentStatus.INPUT_ERROR: False,
        DocumentStatus.FAILED: True,
    }
    for nr, (status, bleibt) in enumerate(erwartet.items()):
        sha = f"{nr:064d}"
        insert_input(db_conn, sha256=sha, content=b"bytes")
        await _schreiben(async_conn, sha, status)
        assert _eingabe(db_conn, sha) is bleibt, status


@pytest.mark.db
async def test_eingabe_und_ergebnis_gehen_in_einer_transaktion(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Scheitert das Loeschen der Eingabe, gilt auch das Ergebnis nicht: Beides
    gilt oder beides nicht. Ein Dokument ohne Eingabe UND ohne Ergebnis darf es
    nicht geben, ebenso wenig eines mit Ergebnis, dessen Eingabe noch liegt.

    Geprueft ohne dem Code zu glauben: Ein Ausloeser auf `inputs` laesst das
    DELETE scheitern. Lagen beide Schritte in getrennten Transaktionen, stuende
    das Ergebnis danach trotzdem da.
    """
    insert_input(db_conn, sha256=HASH, content=b"bytes")
    db_conn.execute(
        "CREATE OR REPLACE FUNCTION _loeschen_verbieten() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'Loeschen verboten'; END; $$ LANGUAGE plpgsql"
    )
    db_conn.execute(
        "CREATE OR REPLACE TRIGGER _loeschen_verbieten_trigger BEFORE DELETE ON inputs "
        "FOR EACH ROW EXECUTE FUNCTION _loeschen_verbieten()"
    )
    try:
        with pytest.raises(Exception, match="Loeschen verboten"):
            await _schreiben(async_conn, raw={"a": 1})
    finally:
        db_conn.execute("DROP TRIGGER IF EXISTS _loeschen_verbieten_trigger ON inputs")

    # Kein Ergebnis, die Eingabe steht: Das Dokument ist offen wie vorher.
    assert get_document(db_conn, HASH) is None
    assert _eingabe(db_conn)


def _schreibvorgaenge_zaehlen(conn: psycopg.Connection[Any]) -> None:
    """Ein Ausloeser, der jedes Anlegen und Aendern an `documents` mitschreibt.

    Das ist die einzige Art, „in einem Schreibvorgang" zu pruefen, ohne dem Code
    zu glauben, den man prueft: Dieselben Endwerte entstehen auch aus fuenf
    einzelnen Anweisungen nacheinander, und dazwischen gaebe es genau den
    Zwischenzustand, den es nicht geben darf.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS _schreibzaehler_db (sha256 text)")
    conn.execute("TRUNCATE _schreibzaehler_db")
    conn.execute(
        "CREATE OR REPLACE FUNCTION _zaehle_db_schreibvorgang() RETURNS trigger AS $$ "
        "BEGIN INSERT INTO _schreibzaehler_db VALUES (NEW.sha256); RETURN NEW; END; "
        "$$ LANGUAGE plpgsql"
    )
    conn.execute(
        "CREATE OR REPLACE TRIGGER _zaehl_db_trigger AFTER INSERT OR UPDATE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION _zaehle_db_schreibvorgang()"
    )


def _schreibvorgaenge(conn: psycopg.Connection[Any]) -> int:
    zaehler = conn.execute(
        "SELECT count(*) FROM _schreibzaehler_db WHERE sha256 = %s", (HASH,)
    ).fetchone()
    assert zaehler is not None
    return int(zaehler[0])


@pytest.mark.db
async def test_endschreiben_legt_die_zeile_in_einem_schreibvorgang_an(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Ohne Zeile legt das Endschreiben sie an, mit allem, in EINEM
    Schreibvorgang: Ergebnis, Angaben zur Eingabe, Herkunft und Vermerke."""
    insert_input(db_conn, sha256=HASH, content=b"bytes")
    _schreibvorgaenge_zaehlen(db_conn)
    try:
        await _schreiben(
            async_conn,
            raw={"markdown": "Text", "content_list": []},
            mime="application/pdf",
            size_bytes=5,
            page_count=2,
            image_tag_at_submit=":17d4955b2",
            notes=[{"kind": "hinweis", "at": "2026-08-08"}],
        )
        assert _schreibvorgaenge(db_conn) == 1
    finally:
        db_conn.execute("DROP TRIGGER IF EXISTS _zaehl_db_trigger ON documents")

    zeile = get_document(db_conn, HASH)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw == {"markdown": "Text", "content_list": []}
    assert (zeile.mime, zeile.size_bytes, zeile.page_count) == ("application/pdf", 5, 2)
    assert zeile.endpoint_id == "testendpoint"
    assert zeile.image_tag_at_submit == ":17d4955b2"
    assert zeile.notes == [{"kind": "hinweis", "at": "2026-08-08"}]
    assert not _eingabe(db_conn)


@pytest.mark.db
async def test_endschreiben_ohne_bekannten_typ_laesst_mime_leer(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Liess sich der Typ nicht bestimmen (oder fehlte die Eingabe), bleiben
    `mime` und `size_bytes` leer, statt erfunden zu werden."""
    await _schreiben(async_conn, status=DocumentStatus.INPUT_ERROR, error="kaputt")
    zeile = get_document(db_conn, HASH)
    assert zeile is not None
    assert zeile.mime is None and zeile.size_bytes is None
    assert zeile.status is DocumentStatus.INPUT_ERROR


@pytest.mark.db
async def test_wecksignal_kommt_bei_jedem_endzustand(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auch `failed` und `input_error` loesen das Signal aus.

    Weckte es nur bei Erfolg, liefe jeder Warter auf ein gescheitertes Dokument
    bis zum Zeitablauf und wuesste am Ende nicht, ob es scheiterte oder dauert.
    """
    for nr, status in enumerate(DocumentStatus):
        sha = f"{nr:064d}"
        db_conn.execute("LISTEN doc_done")
        await _schreiben(async_conn, sha, status, raw={"x": nr})

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
        await _schreiben(async_conn, sha, status, raw={"erste": True})
        with pytest.raises(TerminalOverwriteError):
            await _schreiben(async_conn, sha, DocumentStatus.FAILED, raw={"zweite": True})
        # Und das erste Ergebnis steht unveraendert.
        zeile = get_document(db_conn, sha)
        assert zeile is not None
        assert zeile.raw == {"erste": True}


@pytest.mark.db
async def test_gescheitertes_dokument_darf_ueberschrieben_werden(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auf `failed` schreibt derselbe Aufruf durch — das ist der Wiederanlauf
    (Plan 4.2) und der Grund, warum das Nachsehen im Executor `failed` nicht
    deckt. In einem Schreibvorgang, mit der Zeit der ersten Anlage, und die
    Eingabe, die bei `failed` stand, geht mit dem neuen Ergebnis."""
    insert_input(db_conn, sha256=HASH, content=b"bytes")
    await _schreiben(
        async_conn,
        status=DocumentStatus.FAILED,
        raw={"versuch": 1},
        error="Zeitablauf",
        mime="application/pdf",
        size_bytes=5,
    )
    vorher = get_document(db_conn, HASH)
    assert vorher is not None
    assert _eingabe(db_conn)

    _schreibvorgaenge_zaehlen(db_conn)
    try:
        await _schreiben(async_conn, raw={"versuch": 2})
        assert _schreibvorgaenge(db_conn) == 1
    finally:
        db_conn.execute("DROP TRIGGER IF EXISTS _zaehl_db_trigger ON documents")

    zeile = get_document(db_conn, HASH)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw == {"versuch": 2}
    assert zeile.error is None
    # Was dieser Lauf nicht neu bestimmt hat, bleibt stehen.
    assert (zeile.mime, zeile.size_bytes) == ("application/pdf", 5)
    assert zeile.created_at == vorher.created_at
    assert zeile.updated_at > vorher.updated_at
    assert not _eingabe(db_conn)


@pytest.mark.db
async def test_gleicher_aufruf_zweimal_ist_unschaedlich(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Zweimal derselbe Endzustand wirft nicht — und der ERSTE Schreibvorgang
    gewinnt. Die Vermerke des zweiten werden angehaengt, und eine Eingabe, die
    inzwischen wieder abgelegt wurde, geht.

    Der Massstab ist der Zustand, nicht der Inhalt: `raw` traegt Job-Kennungen
    und Laufzeiten, ist also bei zwei Laeufen nie gleich. Ein Inhaltsvergleich
    waere immer „abweichend" und machte den harmlosen Wiederlauf nach einem
    Absturz im Schreibfenster zum Fehler.
    """
    await _schreiben(async_conn, raw={"lauf": 1})
    insert_input(db_conn, sha256=HASH, content=b"wieder")
    await _schreiben(async_conn, raw={"lauf": 2}, notes=[{"kind": "zweiter"}])
    zeile = get_document(db_conn, HASH)
    assert zeile is not None
    assert zeile.raw == {"lauf": 1}
    assert zeile.notes == [{"kind": "zweiter"}]
    assert not _eingabe(db_conn)


@pytest.mark.db
async def test_nachsehen_und_eingabe_loeschen_fuer_den_executor(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Der Executor liest nur den Zustand (None ohne Ergebnis) und loescht eine
    Eingabe, die neben einem Ergebnis noch steht."""
    assert await get_status_async(async_conn, HASH) is None
    _ergebnis_anlegen(db_conn, status=DocumentStatus.EMPTY)
    insert_input(db_conn, sha256=HASH, content=b"x")
    assert await get_status_async(async_conn, HASH) is DocumentStatus.EMPTY

    await delete_input_async(async_conn, HASH)
    assert not _eingabe(db_conn)
    await delete_input_async(async_conn, HASH)  # ohne Eingabe ein Nichts-Tun


@pytest.mark.db
def test_stand_kennt_fertig_offen_und_unbekannt(db_conn: psycopg.Connection[Any]) -> None:
    """Ergebnis da heisst fertig, Eingabe da heisst offen, keins von beiden
    heisst unbekannt — in der Reihenfolge der Frage, ohne `raw`."""
    fertig, offen, gescheitert, unbekannt = "1" * 64, "2" * 64, "3" * 64, "4" * 64
    _ergebnis_anlegen(db_conn, fertig)
    insert_input(db_conn, sha256=offen, content=b"x")
    _ergebnis_anlegen(db_conn, gescheitert, DocumentStatus.FAILED)
    insert_input(db_conn, sha256=gescheitert, content=b"x")

    stand = read_states(db_conn, [unbekannt, offen, fertig, gescheitert])
    assert [s.sha256 for s in stand] == [unbekannt, offen, fertig, gescheitert]
    assert stand[0].is_unknown and stand[0].row is None
    assert stand[1].is_open and stand[1].row is None
    assert stand[2].is_terminal and stand[2].row is not None
    assert stand[2].row.status is DocumentStatus.DONE
    assert stand[2].row.raw_loaded is False
    # Gescheitert mit stehender Eingabe: Das Ergebnis entscheidet.
    assert stand[3].is_terminal and stand[3].has_input
    assert stand[3].row is not None and stand[3].row.status is DocumentStatus.FAILED


# --- Warten: die Schleife, sync und async ---------------------------------

ARTEN = pytest.mark.parametrize("art", ["sync", "async"])
"""Jeder Fall der Warteschleife laeuft gegen beide Schalen: den sync- und den
async-Generator. Sie teilen den Kern; ein Fall, der nur an einer geprueft waere,
koennte an der anderen still anders ausgehen."""


async def _warten(
    art: str, dsn: str, hashes: list[str], **angaben: Any
) -> tuple[list[DocumentRow], BaseException | None]:
    """Wartet mit der sync- oder der async-Schleife und gibt zurueck, was sie
    lieferte, samt dem Fehler, mit dem sie endete (None ohne Fehler).

    Die sync-Schleife laeuft in einem Faden, damit der Test selbst zugleich
    schreiben und abbrechen kann.
    """
    geliefert: list[DocumentRow] = []
    if art == "sync":

        def lauf() -> BaseException | None:
            try:
                for zeile in wait_for_terminal(dsn, hashes, **angaben):
                    geliefert.append(zeile)
            except Exception as fehler:
                return fehler
            return None

        return geliefert, await asyncio.to_thread(lauf)
    try:
        async for zeile in wait_for_terminal_async(dsn, hashes, **angaben):
            geliefert.append(zeile)
    except Exception as fehler:
        return geliefert, fehler
    return geliefert, None


def _job_endet(
    dsn: str,
    hashes: list[str],
    *,
    signal: bool = True,
    status: DocumentStatus = DocumentStatus.DONE,
) -> None:
    """Was ein Job am Ende tut, fuer mehrere Dokumente in EINER Transaktion:
    Ergebnis anlegen, Eingabe loeschen, und (mit `signal`) das Wecksignal. Ohne
    `signal` sieht es aus wie ein Schreibvorgang von Hand an der Datenbank."""
    with psycopg.connect(dsn, autocommit=True) as job, job.transaction():
        for sha in hashes:
            _ergebnis_anlegen(job, sha, status)
            job.execute("DELETE FROM inputs WHERE sha256 = %s", (sha,))
            if signal:
                job.execute("SELECT pg_notify('doc_done', %s)", (sha,))


def _spaeter(sekunden: float, arbeit: Callable[[], object]) -> threading.Thread:
    """Fuehrt `arbeit` nach `sekunden` in einem eigenen Faden aus."""

    def lauf() -> None:
        time.sleep(sekunden)
        arbeit()

    faden = threading.Thread(target=lauf)
    faden.start()
    return faden


def _nach_der_ersten_lesung(dsn: str, arbeit: Callable[[], object]) -> threading.Thread:
    """Fuehrt `arbeit` in einem eigenen Faden aus, sobald die Warte-Verbindung
    zum ersten Mal gelesen hat — nicht nach einer festen Pause.

    Erst nach der ersten Lesung ist ein Abriss der Verbindung ein Abbruch, der
    durch Neuverbinden heilt; davor ist er ein Betriebsfehler des Aufrufs. Eine
    feste Pause liesse den Test davon abhaengen, wie schnell die Maschine
    verbindet. Gelesen ist, sobald `pg_stat_activity` fuer die Warte-Verbindung
    die Anweisung ueber `documents` und `inputs` als letzte nennt."""

    def lauf() -> None:
        with psycopg.connect(dsn, autocommit=True) as beobachter:
            ende = time.monotonic() + 10.0
            while time.monotonic() < ende:
                zeile = beobachter.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = %s "
                    "AND query LIKE %s",
                    (WAIT_APPLICATION_NAME, "%LEFT JOIN inputs%"),
                ).fetchone()
                if zeile is not None and zeile[0]:
                    break
                time.sleep(0.02)
        arbeit()

    faden = threading.Thread(target=lauf)
    faden.start()
    return faden


def _warte_verbindungen(dsn: str) -> int:
    """Wie viele Warte-Verbindungen gerade offen sind. Ein Backend braucht nach
    dem Schliessen einen Augenblick, bis es aus `pg_stat_activity` verschwindet;
    deshalb wird bis zu zwei Sekunden auf null gewartet."""
    with psycopg.connect(dsn, autocommit=True) as beobachter:
        ende = time.monotonic() + 2.0
        while True:
            zeile = beobachter.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND application_name = %s",
                (WAIT_APPLICATION_NAME,),
            ).fetchone()
            anzahl = int(zeile[0]) if zeile is not None else 0
            if anzahl == 0 or time.monotonic() > ende:
                return anzahl
            time.sleep(0.05)


def _warte_verbindung_kappen(dsn: str) -> bool:
    """Beendet die Warte-Verbindung von der Server-Seite, wie ein Neustart von
    Postgres oder ein abgerissenes Netz es taete."""
    with psycopg.connect(dsn, autocommit=True) as admin:
        zeile = admin.execute(
            "SELECT bool_or(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name = %s",
            (WAIT_APPLICATION_NAME,),
        ).fetchone()
    return bool(zeile and zeile[0])


def _job_anlegen(conn: psycopg.Connection[Any], sha: str) -> None:
    """Ein wartender Job zum Dokument, damit sich zeigen laesst, dass Frist und
    Abbruch ihn nicht anfassen."""
    conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, lock, queueing_lock) "
        "VALUES ('ocr', 'ocr_document', %s, %s)",
        (sha, sha),
    )


def _bestand(conn: psycopg.Connection[Any]) -> tuple[list[Any], list[Any], list[Any]]:
    """Jobs, Eingaben und Ergebnisse, so wie sie gerade stehen."""
    return (
        conn.execute(
            "SELECT id, status, attempts, abort_requested FROM procrastinate_jobs ORDER BY id"
        ).fetchall(),
        conn.execute("SELECT sha256 FROM inputs ORDER BY sha256").fetchall(),
        conn.execute("SELECT sha256, status FROM documents ORDER BY sha256").fetchall(),
    )


@pytest.mark.db
@ARTEN
async def test_warten_liefert_fertiges_sofort_und_ohne_raw(
    art: str,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
) -> None:
    """Sind die Dokumente schon endgueltig, liefert die Schleife sie sofort,
    statt auf ein Signal zu warten, das schon vorbei ist — jedes einmal, auch wenn
    es zweimal gefragt ist, und ohne `raw`. Gescheitert ist ein Ergebnis wie
    jedes andere."""
    fertig, gescheitert = "1" * 64, "2" * 64
    await _schreiben(async_conn, fertig, raw={"gross": "x" * 1000})
    await _schreiben(async_conn, gescheitert, DocumentStatus.FAILED, error="aus")

    begonnen = time.monotonic()
    geliefert, fehler = await _warten(
        art, _test_datenbank, [fertig, gescheitert, fertig], timeout=30.0
    )

    assert fehler is None
    assert time.monotonic() - begonnen < 1.0
    assert sorted(z.sha256 for z in geliefert) == [fertig, gescheitert]
    assert all(z.raw is None and z.raw_loaded is False for z in geliefert)
    assert _warte_verbindungen(_test_datenbank) == 0


@pytest.mark.db
@ARTEN
async def test_warten_meldet_einen_unbekannten_hash_sofort(
    art: str, db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Weder Ergebnis noch Eingabe: `DocumentNotFound` kommt sofort, nicht nach
    Ablauf der Frist, und nennt den unbekannten Hash."""
    offen, unbekannt = "3" * 64, "d" * 64
    insert_input(db_conn, sha256=offen, content=b"x")

    begonnen = time.monotonic()
    geliefert, fehler = await _warten(
        art, _test_datenbank, [offen, unbekannt], timeout=30.0
    )

    assert isinstance(fehler, DocumentNotFound)
    assert unbekannt in str(fehler) and offen not in str(fehler)
    assert time.monotonic() - begonnen < 1.0
    assert geliefert == []
    assert _warte_verbindungen(_test_datenbank) == 0


@pytest.mark.db
@ARTEN
async def test_frist_nennt_die_offenen_und_fasst_keinen_job_an(
    art: str,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
) -> None:
    """Die Frist gilt fuer den ganzen Aufruf. Was bis dahin fertig ist, ist
    geliefert; der `TimeoutError` nennt die Hashes, die noch offen sind, in der
    Reihenfolge der Frage. Jobs, Eingaben und Ergebnisse bleiben, wie sie sind."""
    fertig, offen1, offen2 = "1" * 64, "5" * 64, "4" * 64
    await _schreiben(async_conn, fertig)
    for sha in (offen1, offen2):
        insert_input(db_conn, sha256=sha, content=b"x")
        _job_anlegen(db_conn, sha)
    vorher = _bestand(db_conn)

    begonnen = time.monotonic()
    geliefert, fehler = await _warten(
        art, _test_datenbank, [fertig, offen1, offen2], timeout=1.0, poll_seconds=0.3
    )

    assert 0.9 < time.monotonic() - begonnen < 3.0
    assert [z.sha256 for z in geliefert] == [fertig]
    assert isinstance(fehler, WaitTimeoutError)
    assert isinstance(fehler, TimeoutError)
    assert fehler.pending == (offen1, offen2)
    assert offen1 in str(fehler) and offen2 in str(fehler) and fertig not in str(fehler)
    assert "Geduld" in str(fehler)
    assert _bestand(db_conn) == vorher
    assert _warte_verbindungen(_test_datenbank) == 0


@pytest.mark.db
@ARTEN
async def test_ohne_signal_findet_der_takt_das_ergebnis(
    art: str, db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Auch ohne Signal — etwa bei einem Schreibvorgang von Hand an der
    Datenbank — endet das Warten: Im Takt werden alle offenen gelesen."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    faden = _spaeter(0.5, lambda: _job_endet(_test_datenbank, [HASH], signal=False))
    try:
        geliefert, fehler = await _warten(
            art, _test_datenbank, [HASH], timeout=10.0, poll_seconds=0.3
        )
    finally:
        faden.join()

    assert fehler is None
    assert [z.sha256 for z in geliefert] == [HASH]


@pytest.mark.db
@ARTEN
async def test_kein_signal_eines_pakets_geht_verloren(
    art: str, db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Fuenf Dokumente enden in EINER Transaktion, ihre fuenf Signale kommen in
    einem Paket. Der Takt steht auf einer Minute: Jedes Dokument muss ueber sein
    Signal kommen. Ein `break` im Generator der Signale verwuerfe den Rest des
    Pakets, und die uebrigen vier warteten bis zur Frist."""
    hashes = [str(n) * 64 for n in range(5, 10)]
    for sha in hashes:
        insert_input(db_conn, sha256=sha, content=b"x")
    faden = _spaeter(0.5, lambda: _job_endet(_test_datenbank, hashes))
    begonnen = time.monotonic()
    try:
        geliefert, fehler = await _warten(
            art, _test_datenbank, hashes, timeout=10.0, poll_seconds=60.0
        )
    finally:
        faden.join()

    assert fehler is None
    assert sorted(z.sha256 for z in geliefert) == sorted(hashes)
    assert time.monotonic() - begonnen < 5.0


@pytest.mark.db
@ARTEN
async def test_nach_einem_abbruch_der_verbindung_wird_neu_verbunden(
    art: str, db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Reisst die Warte-Verbindung ab, verbindet die Schleife neu, lauscht wieder
    und liest alles Offene. Der Takt steht auf einer Minute: Das Ergebnis kommt
    ueber das Lesen nach dem Neuverbinden oder ueber das Signal danach, nie ueber
    den Takt."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    gekappt: list[bool] = []

    def kappen_dann_enden() -> None:
        gekappt.append(_warte_verbindung_kappen(_test_datenbank))
        time.sleep(0.3)
        _job_endet(_test_datenbank, [HASH])

    faden = _nach_der_ersten_lesung(_test_datenbank, kappen_dann_enden)
    begonnen = time.monotonic()
    try:
        geliefert, fehler = await _warten(
            art, _test_datenbank, [HASH], timeout=10.0, poll_seconds=60.0
        )
    finally:
        faden.join()

    assert gekappt == [True]
    assert fehler is None
    assert [z.sha256 for z in geliefert] == [HASH]
    assert time.monotonic() - begonnen < 5.0
    assert _warte_verbindungen(_test_datenbank) == 0


@pytest.mark.db
@ARTEN
async def test_ein_zugangsfehler_beim_neu_verbinden_beendet_das_warten(
    art: str,
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neu verbunden wird nur bei wiederholbaren Fehlern. Taugt der Zugang beim
    Neuverbinden nicht (hier das falsche Passwort, SQLSTATE 28P01), endet das
    Warten mit `ConfigurationError`, statt es bis zur Frist weiter zu versuchen.
    Der Fehler kommt als echter psycopg-Fehler aus dem Verbinden, damit er durch
    dieselbe Uebersetzung laeuft wie im Betrieb."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    aufrufe = _warte_verbinden_ersetzen(
        monkeypatch,
        art,
        lambda n: psycopg.errors.InvalidPassword("password authentication failed")
        if n > 1
        else None,
    )
    faden = _nach_der_ersten_lesung(
        _test_datenbank, lambda: _warte_verbindung_kappen(_test_datenbank)
    )
    begonnen = time.monotonic()
    try:
        _, fehler = await _warten(
            art, _test_datenbank, [HASH], timeout=10.0, poll_seconds=60.0
        )
    finally:
        faden.join()

    assert isinstance(fehler, ConfigurationError)
    assert "28P01" in str(fehler)
    assert aufrufe["n"] == 2
    assert time.monotonic() - begonnen < 3.0


@pytest.mark.db
@ARTEN
async def test_scheitert_das_neu_verbinden_voruebergehend_wird_es_wieder_versucht(
    art: str,
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startet Postgres gerade neu, scheitert auch das Neuverbinden — ohne
    SQLSTATE, wie eine abgelehnte Verbindung. Das ist wiederholbar: Die Schleife
    wartet kurz, versucht es wieder und liefert das Ergebnis."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    aufrufe = _warte_verbinden_ersetzen(
        monkeypatch,
        art,
        lambda n: psycopg.OperationalError("connection failed: Connection refused")
        if n == 2
        else None,
    )

    def kappen_dann_enden() -> None:
        _warte_verbindung_kappen(_test_datenbank)
        _job_endet(_test_datenbank, [HASH])

    faden = _nach_der_ersten_lesung(_test_datenbank, kappen_dann_enden)
    try:
        geliefert, fehler = await _warten(
            art, _test_datenbank, [HASH], timeout=10.0, poll_seconds=60.0
        )
    finally:
        faden.join()

    assert fehler is None
    assert [z.sha256 for z in geliefert] == [HASH]
    assert aufrufe["n"] == 3
    assert _warte_verbindungen(_test_datenbank) == 0


@pytest.mark.db
@ARTEN
async def test_die_frist_nennt_kein_dokument_das_beim_langsamen_verbraucher_fertig_wurde(
    art: str,
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    _test_datenbank: str,
) -> None:
    """Haelt sich der Verbraucher beim ersten Dokument laenger auf als die Frist,
    und wird das zweite derweil fertig, nennt die Frist es nicht als offen: Vor
    dem Zeitablauf wird ein letztes Mal gelesen, und das zweite kommt als
    Ergebnis."""
    fertig, spaet = "1" * 64, "5" * 64
    await _schreiben(async_conn, fertig)
    insert_input(db_conn, sha256=spaet, content=b"x")

    geliefert: list[str] = []
    fehler: BaseException | None = None
    if art == "sync":
        zeilen = wait_for_terminal(_test_datenbank, [fertig, spaet], timeout=1.0, poll_seconds=60.0)
        try:
            for zeile in zeilen:
                geliefert.append(zeile.sha256)
                if zeile.sha256 == fertig:
                    _job_endet(_test_datenbank, [spaet])
                    time.sleep(1.3)
        except WaitTimeoutError as abgelaufen:
            fehler = abgelaufen
    else:
        async_zeilen = wait_for_terminal_async(
            _test_datenbank, [fertig, spaet], timeout=1.0, poll_seconds=60.0
        )
        try:
            async for zeile in async_zeilen:
                geliefert.append(zeile.sha256)
                if zeile.sha256 == fertig:
                    await asyncio.to_thread(_job_endet, _test_datenbank, [spaet])
                    await asyncio.sleep(1.3)
        except WaitTimeoutError as abgelaufen:
            fehler = abgelaufen

    assert fehler is None
    assert geliefert == [fertig, spaet]
    assert _warte_verbindungen(_test_datenbank) == 0


class _StoerrischeWarteVerbindung:
    """Eine Warte-Verbindung ohne Datenbank: Lauschen gelingt, Signale kommen
    keine, und schliessen geht. Was gelesen wird, entscheidet der Test."""

    def execute(self, sql: object, *args: object, **kwargs: object) -> None:
        return None

    def notifies(self, *, timeout: float, stop_after: int) -> Any:
        time.sleep(min(timeout, 0.05))
        return iter(())

    def close(self) -> None:
        return None


class _AsyncStoerrischeWarteVerbindung:
    """Dasselbe asynchron."""

    async def execute(self, sql: object, *args: object, **kwargs: object) -> None:
        return None

    async def notifies(self, *, timeout: float, stop_after: int) -> Any:
        await asyncio.sleep(min(timeout, 0.05))
        keine: tuple[psycopg.Notify, ...] = ()
        for meldung in keine:
            yield meldung

    async def close(self) -> None:
        return None


@ARTEN
async def test_ein_wiederkehrender_abbruch_haelt_die_frist_und_bestuermt_die_datenbank_nicht(
    art: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reisst jede neue Verbindung gleich bei ihrer ersten Lesung wieder ab (etwa
    ein `statement_timeout`, den die Lesung reisst), verbindet die Schleife nicht
    im Leerlauf neu: Vor jedem neuen Versuch wartet sie kurz, und die Frist endet
    das Warten mit `WaitTimeoutError`. Mehr als eine Handvoll Versuche in einer
    Sekunde waeren ein Sturm auf die Datenbank."""
    from mineru_ocr import db

    verbindungen = {"n": 0}
    lesungen = {"n": 0}

    def zaehlen() -> None:
        verbindungen["n"] += 1
        if verbindungen["n"] > 50:
            raise RuntimeError("Sturm: mehr als 50 Verbindungen")

    def lesen() -> list[DocumentState]:
        lesungen["n"] += 1
        if lesungen["n"] > 1:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        return [DocumentState(sha256=HASH, row=None, has_input=True)]

    if art == "sync":

        def verbinden(dsn: str, **angaben: Any) -> Any:
            zaehlen()
            return _StoerrischeWarteVerbindung()

        monkeypatch.setattr(db, "_warte_verbindung", verbinden)
        monkeypatch.setattr(db, "read_states", lambda conn, hashes: lesen())
    else:

        async def verbinden_async(dsn: str, **angaben: Any) -> Any:
            zaehlen()
            return _AsyncStoerrischeWarteVerbindung()

        async def lesen_async(conn: Any, hashes: Any) -> list[DocumentState]:
            return lesen()

        monkeypatch.setattr(db, "_warte_verbindung_async", verbinden_async)
        monkeypatch.setattr(db, "read_states_async", lesen_async)

    begonnen = time.monotonic()
    geliefert, fehler = await _warten(art, "dbname=gibtesnicht", [HASH], timeout=1.0, poll_seconds=0.2)

    assert geliefert == []
    assert isinstance(fehler, WaitTimeoutError), fehler
    assert fehler.pending == (HASH,)
    assert time.monotonic() - begonnen < 2.0
    assert verbindungen["n"] <= 5


@ARTEN
async def test_die_frist_begrenzt_auch_das_verbinden(art: str) -> None:
    """Antwortet der Rechner der Datenbank gar nicht (die Route ist weg), haengt
    das Verbinden nicht bis zur Voreinstellung des Betriebssystems: Die Frist
    begrenzt es. 192.0.2.1 ist eine Dokumentationsadresse (RFC 5737), unter der
    niemand antwortet. Das erste Verbinden scheitert, also `DatabaseError`."""
    ergebnis: dict[str, BaseException | None] = {}

    def lauf() -> None:
        ergebnis["fehler"] = asyncio.run(
            _warten(art, "host=192.0.2.1 port=5432 dbname=x user=x", [HASH], timeout=1.0)
        )[1]

    faden = threading.Thread(target=lauf, daemon=True)
    faden.start()
    faden.join(timeout=8.0)

    assert not faden.is_alive(), "das Verbinden haelt die Frist nicht"
    assert isinstance(ergebnis["fehler"], DatabaseError)


@ARTEN
async def test_eine_frist_die_keine_zahl_ist_wird_abgewiesen(art: str) -> None:
    """`nan` liefe nie ab, eine negative Frist ist ein Irrtum: beides `ValueError`,
    bevor verbunden wird."""
    for frist in (float("nan"), -1.0):
        geliefert, fehler = await _warten(art, "dbname=gibtesnicht", [HASH], timeout=frist)
        assert geliefert == []
        assert isinstance(fehler, ValueError), (frist, fehler)


@pytest.mark.db
@ARTEN
async def test_eine_unendliche_frist_ist_keine_frist(
    art: str, async_conn: psycopg.AsyncConnection[Any], _test_datenbank: str
) -> None:
    """`inf` ist eine gueltige Frist und heisst unbegrenzt — auch fuers
    Verbinden, dem sich keine unendliche Sekundenzahl mitgeben laesst."""
    await _schreiben(async_conn, HASH)
    geliefert, fehler = await _warten(art, _test_datenbank, [HASH], timeout=float("inf"))
    assert fehler is None
    assert [z.sha256 for z in geliefert] == [HASH]


@ARTEN
async def test_ein_einzelner_hash_ist_keine_folge_von_hashes(art: str) -> None:
    """Auch die Schleife selbst weist einen einzelnen Hash ab — fuer jeden
    Aufrufer, nicht nur fuer den Client."""
    geliefert, fehler = await _warten(art, "dbname=gibtesnicht", HASH)  # type: ignore[arg-type]
    assert geliefert == []
    assert isinstance(fehler, TypeError)


def _warte_verbinden_ersetzen(
    monkeypatch: pytest.MonkeyPatch,
    art: str,
    ersatz: Callable[[int], BaseException | None],
) -> dict[str, int]:
    """Ersetzt psycopgs Verbinden fuer die Warte-Verbindungen (erkannt an ihrem
    `application_name`): Jeder Versuch wird gezaehlt, und `ersatz(n)` gibt fuer
    den n-ten Versuch den Fehler zurueck, mit dem er scheitert, oder None. Alle
    anderen Verbindungen des Tests gehen unveraendert durch. So laeuft der Fehler
    durch dieselbe Uebersetzung wie ein echter."""
    aufrufe = {"n": 0}

    def pruefen(angaben: dict[str, Any]) -> None:
        if angaben.get("application_name") != WAIT_APPLICATION_NAME:
            return
        aufrufe["n"] += 1
        fehler = ersatz(aufrufe["n"])
        if fehler is not None:
            raise fehler

    if art == "sync":
        echt = psycopg.connect

        def verbinden(dsn: str, **angaben: Any) -> Any:
            pruefen(angaben)
            return echt(dsn, **angaben)

        monkeypatch.setattr(psycopg, "connect", verbinden)
    else:
        echt_async = psycopg.AsyncConnection.connect

        async def verbinden_async(dsn: str, **angaben: Any) -> Any:
            pruefen(angaben)
            return await echt_async(dsn, **angaben)

        monkeypatch.setattr(psycopg.AsyncConnection, "connect", verbinden_async)
    return aufrufe


class _JobEndetNachDerErstenLesung:
    """Eine Verbindung, nach deren erster Lesung von `documents` der Job endet,
    und die mitschreibt, welche Anweisungen sie bekam.

    Damit faellt der Augenblick, in dem das Ergebnis entsteht und die Eingabe
    geht, genau hinter die erste Anweisung, die nach einem Ergebnis fragt. Wer
    `documents` und `inputs` in zwei Anweisungen liest, sieht dort weder
    Ergebnis noch Eingabe und meldet ein unbekanntes Dokument. Eine Anweisung
    sieht beide im selben Schnappschuss: zuerst offen, danach fertig.
    """

    def __init__(self, echte: Any, dsn: str) -> None:
        self._echte = echte
        self._dsn = dsn
        self.ausgeloest = False
        self.anweisungen: list[str] = []

    def _danach(self, sql: object) -> None:
        text = sql.as_string(None) if isinstance(sql, psycopg.sql.Composable) else str(sql)
        self.anweisungen.append(text)
        if self.ausgeloest or "documents" not in text:
            return
        self.ausgeloest = True
        _job_endet(self._dsn, [HASH])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._echte, name)


class _SyncJobEndet(_JobEndetNachDerErstenLesung):
    def execute(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        ergebnis = self._echte.execute(sql, *args, **kwargs)
        self._danach(sql)
        return ergebnis

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        verbindung = self
        echter = self._echte.cursor(*args, **kwargs)

        class _Cursor:
            def __enter__(self) -> Any:
                echter.__enter__()
                return self

            def __exit__(self, *fehler: Any) -> None:
                echter.__exit__(*fehler)

            def execute(self, sql: Any, *a: Any, **k: Any) -> Any:
                echter.execute(sql, *a, **k)
                verbindung._danach(sql)
                return echter

        return _Cursor()


class _AsyncJobEndet(_JobEndetNachDerErstenLesung):
    async def execute(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        ergebnis = await self._echte.execute(sql, *args, **kwargs)
        await asyncio.to_thread(self._danach, sql)
        return ergebnis

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        verbindung = self
        echter = self._echte.cursor(*args, **kwargs)

        class _Cursor:
            async def __aenter__(self) -> Any:
                await echter.__aenter__()
                return self

            async def __aexit__(self, *fehler: Any) -> None:
                await echter.__aexit__(*fehler)

            async def execute(self, sql: Any, *a: Any, **k: Any) -> Any:
                await echter.execute(sql, *a, **k)
                await asyncio.to_thread(verbindung._danach, sql)
                return echter

            def __getattr__(self, name: str) -> Any:
                return getattr(echter, name)

        return _Cursor()


@pytest.mark.db
@ARTEN
async def test_warten_haelt_ein_dokument_nicht_fuer_unbekannt_und_laedt_weder_raw_noch_content(
    art: str,
    db_conn: psycopg.Connection[Any],
    _test_datenbank: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endet der Job unmittelbar nach der ersten Lesung, liefert das Warten das
    Ergebnis — und kein `DocumentNotFound`, wie es zwei Abfragen nacheinander
    (erst `documents`, dann `inputs`) taeten. Dabei fragt keine Anweisung nach
    `raw` oder `content`."""
    from mineru_ocr import db

    insert_input(db_conn, sha256=HASH, content=b"x")
    verbindungen: list[_JobEndetNachDerErstenLesung] = []
    if art == "sync":
        echte_sync = db._warte_verbindung

        def verbinden(dsn: str, **angaben: Any) -> Any:
            verbindungen.append(_SyncJobEndet(echte_sync(dsn, **angaben), dsn))
            return verbindungen[-1]

        monkeypatch.setattr(db, "_warte_verbindung", verbinden)
    else:
        echte_async = db._warte_verbindung_async

        async def verbinden_async(dsn: str, **angaben: Any) -> Any:
            verbindungen.append(_AsyncJobEndet(await echte_async(dsn, **angaben), dsn))
            return verbindungen[-1]

        monkeypatch.setattr(db, "_warte_verbindung_async", verbinden_async)

    geliefert, fehler = await _warten(
        art, _test_datenbank, [HASH], timeout=10.0, poll_seconds=60.0
    )

    assert fehler is None
    assert [z.sha256 for z in geliefert] == [HASH]
    (verbindung,) = verbindungen
    assert verbindung.ausgeloest
    assert verbindung.anweisungen[0].startswith("LISTEN")
    for anweisung in verbindung.anweisungen:
        assert "content" not in anweisung
        assert "d.raw" not in anweisung


@pytest.mark.db
def test_strg_c_beendet_nur_das_warten(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Strg-C kommt im sync-Warten nach Bruchteilen einer Sekunde an, auch wenn
    der Takt auf einer Minute steht. Die Warte-Verbindung ist danach zu; Job,
    Eingabe und Ergebnisse bleiben, wie sie sind."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    _job_anlegen(db_conn, HASH)
    vorher = _bestand(db_conn)

    def unterbrechen(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    bisher = signal.signal(signal.SIGALRM, unterbrechen)
    begonnen = time.monotonic()
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.5)
        with pytest.raises(KeyboardInterrupt):
            for _ in wait_for_terminal(_test_datenbank, [HASH], poll_seconds=60.0):
                pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, bisher)

    assert time.monotonic() - begonnen < 2.0
    assert _warte_verbindungen(_test_datenbank) == 0
    assert _bestand(db_conn) == vorher


@pytest.mark.db
async def test_abbrechen_beendet_nur_das_async_warten(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Das Gegenstueck zu Strg-C im async-Warten ist `cancel()`: Es kommt sofort
    an, die Warte-Verbindung ist danach zu, und nichts wird angefasst."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    _job_anlegen(db_conn, HASH)
    vorher = _bestand(db_conn)

    async def warten() -> None:
        async for _ in wait_for_terminal_async(_test_datenbank, [HASH], poll_seconds=60.0):
            pass

    aufgabe = asyncio.create_task(warten())
    await asyncio.sleep(0.5)
    begonnen = time.monotonic()
    aufgabe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await aufgabe

    assert time.monotonic() - begonnen < 1.0
    assert _warte_verbindungen(_test_datenbank) == 0
    assert _bestand(db_conn) == vorher


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
    await _schreiben(async_conn, raw={"gross": "x" * 1000})

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
    """Der Zustandsfilter waehlt aus, ohne dass der Aufrufer selbst sieben muss.
    Offene Dokumente haben keine Zeile und erscheinen nicht."""
    await _schreiben(async_conn, f"{0:064d}", DocumentStatus.DONE)
    await _schreiben(async_conn, f"{1:064d}", DocumentStatus.FAILED)
    await _schreiben(async_conn, f"{2:064d}", DocumentStatus.FAILED)
    insert_input(db_conn, sha256=f"{3:064d}", content=b"offen")

    assert len(list_documents(db_conn, status=DocumentStatus.DONE)) == 1
    assert len(list_documents(db_conn, status=DocumentStatus.FAILED)) == 2
    assert len(list_documents(db_conn)) == 3
    assert len(list_documents(db_conn, limit=2)) == 2


@pytest.mark.db
async def test_zaehlung_kennt_jeden_zustand_und_die_offenen(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Auch Zustaende ohne Zeilen kommen mit Null vor — ein fehlender
    Schluessel wuerde die Statusausgabe genau dann sprengen, wenn alles gut
    lief. Offen ist, was eine Eingabe und kein Ergebnis hat; ein gescheitertes
    Dokument mit stehender Eingabe zaehlt unter `failed`, nicht doppelt."""
    zaehlung = counts_by_status(db_conn)
    assert set(zaehlung) == set(DocumentStatus)
    assert all(anzahl == 0 for anzahl in zaehlung.values())
    assert count_open(db_conn) == 0

    insert_input(db_conn, sha256="1" * 64, content=b"offen")
    insert_input(db_conn, sha256="2" * 64, content=b"gescheitert")
    await _schreiben(async_conn, "2" * 64, DocumentStatus.FAILED)
    assert count_open(db_conn) == 1
    assert counts_by_status(db_conn)[DocumentStatus.FAILED] == 1


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


@pytest.mark.db
def test_alter_herzschlag_zaehlt_nicht_als_laufender_daemon(
    db_conn: psycopg.Connection[Any],
    abgestuerzter_worker: Callable[[], None],
) -> None:
    """Nach einem Absturz bleibt die Worker-Zeile stehen, und ihr Alter ist das
    Merkmal: Ein Herzschlag aelter als die Schwelle ist kein laufender Daemon.

    Der Fall, der das ausgeloest hat: eine 67 Tage alte Zeile liess `status`
    „laeuft" sagen.
    """
    abgestuerzter_worker()
    assert worker_heartbeats(db_conn) == []

    # Ein frischer Herzschlag daneben zaehlt, der alte weiterhin nicht.
    db_conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - interval '5 seconds')"
    )
    lebende = worker_heartbeats(db_conn)
    assert len(lebende) == 1


@pytest.mark.db
def test_die_schwelle_liegt_zwischen_20_und_40_sekunden(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Die Grenze: Ein 20 Sekunden alter Herzschlag liegt unter der Schwelle von
    30 s und zaehlt, ein 40 Sekunden alter liegt darueber und zaehlt nicht."""
    db_conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - interval '20 seconds')"
    )
    assert len(worker_heartbeats(db_conn)) == 1

    db_conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - interval '40 seconds')"
    )
    assert len(worker_heartbeats(db_conn)) == 1


@pytest.mark.db
def test_das_alter_rechnet_mit_der_laufenden_uhr_der_datenbank(
    db_conn: psycopg.Connection[Any],
) -> None:
    """In einer offenen Transaktion bleibt `now()` auf deren Beginn stehen,
    `clock_timestamp()` laeuft weiter. Ein Herzschlag, der beim Einfuegen 28 s alt
    war, ist zweieinhalb Sekunden spaeter aelter als die Schwelle — und das muss
    die Abfrage sehen. Mit `now()` bliebe er fuer immer 28 s alt, und ein
    Client, der eine Transaktion offen haelt, saehe einen toten Daemon als
    lebend.

    Der Spielraum: Die erste Pruefung hat zwei Sekunden Luft gegen eine langsame
    Maschine, die zweite kann nur zu spaet, nie zu frueh kommen (`pg_sleep`
    schlaeft mindestens so lange)."""
    with db_conn.transaction():
        db_conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) "
            "VALUES (clock_timestamp() - interval '28 seconds')"
        )
        assert len(worker_heartbeats(db_conn)) == 1

        db_conn.execute("SELECT pg_sleep(2.5)")
        assert worker_heartbeats(db_conn) == []


@pytest.mark.db
def test_eine_unlesbare_worker_tabelle_laesst_eine_offene_transaktion_benutzbar(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Die Lebendigkeit ist nur Auskunft: Scheitert ihre Abfrage in einer
    Transaktion des Aufrufers (`client.conn.transaction()`), bricht PostgreSQL
    die ganze Transaktion ab, und jede Anweisung danach scheitert mit
    `InFailedSqlTransaction` — auch wenn der Aufrufer den `DatabaseError` faengt.
    Die Abfrage laeuft deshalb in einem Sicherungspunkt, und nur der wird
    zurueckgerollt."""
    db_conn.execute(
        "ALTER TABLE procrastinate_workers RENAME TO procrastinate_workers_weg"
    )
    try:
        with db_conn.transaction():
            with pytest.raises(DatabaseError):
                worker_heartbeats(db_conn)
            assert db_conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        db_conn.execute(
            "ALTER TABLE procrastinate_workers_weg RENAME TO procrastinate_workers"
        )


def test_die_schwelle_ist_procrastinates_grenze_fuer_haengengebliebene_worker() -> None:
    """Die Schwelle ist Procrastinates eigene Grenze fuer „gilt als haengengeblieben"
    (`stalled_worker_timeout`) und deckt mindestens drei Herzschlag-Abstaende
    (`update_heartbeat_interval`): Ein einzelner verspaeteter Herzschlag macht den
    Daemon nicht tot. Gelesen wird die Voreinstellung der installierten Fassung —
    der Daemon aendert sie nicht —, damit ein Upgrade, das sie verschiebt, hier
    auffaellt und nicht erst im Betrieb."""
    parameter = inspect.signature(procrastinate.worker.Worker.__init__).parameters
    assert DAEMON_HEARTBEAT_MAX_AGE_SECONDS == parameter["stalled_worker_timeout"].default
    assert DAEMON_HEARTBEAT_MAX_AGE_SECONDS >= 3 * parameter["update_heartbeat_interval"].default
