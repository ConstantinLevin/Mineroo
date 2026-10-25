"""Die Tabellen: Schreibregeln, Wecksignal, Stand und abgeleiteter Zustand.

Die Tests zum abgeleiteten Zustand sind REIN und brauchen keine Datenbank — sie
bekommen Ergebnis und Job-Liste hereingereicht. Das ist der Grund, warum
`derive_status` eine reine Funktion ist: Die Regel ist der heikle Teil, die
Abfrage ist Handwerk.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import procrastinate.worker
import psycopg
import pytest
from procrastinate.jobs import Job

from mineru_ocr.config import DAEMON_HEARTBEAT_MAX_AGE_SECONDS
from mineru_ocr.db import (
    DocumentRow,
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
    worker_heartbeats,
    write_terminal_async,
)
from mineru_ocr.errors import DatabaseError, DocumentNotFound, TerminalOverwriteError

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


@pytest.mark.db
async def test_warten_findet_bereits_fertiges_dokument_sofort(
    db_conn: psycopg.Connection[Any], async_conn: psycopg.AsyncConnection[Any]
) -> None:
    """Ist das Dokument schon endgueltig, kehrt `wait_for_terminal` sofort
    zurueck, statt auf ein Signal zu warten, das schon vorbei ist — und laedt
    dabei `raw` nicht."""
    await _schreiben(async_conn, raw={"fertig": True})
    begonnen = time.monotonic()
    zeile = wait_for_terminal(db_conn, HASH, timeout=30.0)
    assert time.monotonic() - begonnen < 1.0
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is None and zeile.raw_loaded is False


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
    """Bleibt das Dokument offen, endet das Warten mit TimeoutError.

    Und die Meldung sagt, was der Zeitablauf bedeutet: etwas ueber die Geduld
    des Wartenden, nichts ueber das Dokument.
    """
    insert_input(db_conn, sha256=HASH, content=b"x")
    with pytest.raises(TimeoutError) as fehler:
        wait_for_terminal(db_conn, HASH, timeout=1.0, poll_seconds=0.3)
    assert "Geduld" in str(fehler.value)


@pytest.mark.db
def test_warten_faellt_auf_abfragen_zurueck(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Auch ohne Signal — etwa bei einem Schreibvorgang von aussen — endet das
    Warten durch die Rueckfallabfrage.

    Geprueft wird das, indem das Ergebnis mit ROHEM SQL angelegt wird, ganz ohne
    `pg_notify`. Genau so sieht ein Schreibvorgang von Hand an der Datenbank aus,
    und genau dagegen ist die Rueckfallabfrage da.
    """
    insert_input(db_conn, sha256=HASH, content=b"x")

    def spaeter_setzen() -> None:
        time.sleep(0.5)
        with psycopg.connect(_test_datenbank, autocommit=True) as andere:
            _ergebnis_anlegen(andere)
            andere.execute("DELETE FROM inputs WHERE sha256 = %s", (HASH,))

    faden = threading.Thread(target=spaeter_setzen)
    faden.start()
    try:
        zeile = wait_for_terminal(db_conn, HASH, timeout=10.0, poll_seconds=0.2)
    finally:
        faden.join()
    assert zeile.status is DocumentStatus.DONE


class _JobEndetNachDerErstenLesung:
    """Eine Verbindung, nach deren erster Lesung von `documents` der Job endet.

    Damit faellt der Augenblick, in dem das Ergebnis entsteht und die Eingabe
    geht, genau hinter die erste Anweisung, die nach einem Ergebnis fragt. Wer
    `documents` und `inputs` in zwei Anweisungen liest, sieht dort weder
    Ergebnis noch Eingabe und meldet ein unbekanntes Dokument. Eine Anweisung
    sieht beide im selben Schnappschuss: zuerst offen, danach fertig.
    """

    def __init__(self, echte: psycopg.Connection[Any], dsn: str) -> None:
        self._echte = echte
        self._dsn = dsn
        self.ausgeloest = False

    def _danach(self, sql: object) -> None:
        if self.ausgeloest or "documents" not in str(sql):
            return
        self.ausgeloest = True
        with psycopg.connect(self._dsn, autocommit=True) as job:
            _ergebnis_anlegen(job)
            job.execute("DELETE FROM inputs WHERE sha256 = %s", (HASH,))
            job.execute("SELECT pg_notify('doc_done', %s)", (HASH,))

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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._echte, name)


@pytest.mark.db
def test_warten_haelt_ein_dokument_nicht_fuer_unbekannt_das_zwischen_zwei_lesungen_fertig_wird(
    db_conn: psycopg.Connection[Any], _test_datenbank: str
) -> None:
    """Endet der Job unmittelbar nach der ersten Lesung, liefert das Warten das
    Ergebnis — und kein `DocumentNotFound`, wie es zwei Abfragen nacheinander
    (erst `documents`, dann `inputs`) taeten."""
    insert_input(db_conn, sha256=HASH, content=b"x")
    verbindung = _JobEndetNachDerErstenLesung(db_conn, _test_datenbank)

    zeile = wait_for_terminal(verbindung, HASH, timeout=10.0, poll_seconds=0.2)  # type: ignore[arg-type]

    assert verbindung.ausgeloest
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
