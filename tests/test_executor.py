"""Der Ablauf an einem Dokument — ohne Warteschlange, ohne Netz.

Dass es diese Datei gibt, ist der Existenzgrund von `executor.py`: Das Modul
weiß nichts von Procrastinate, damit genau diese Tests ohne laufende
Warteschlange möglich sind. Fehlten sie, wäre die Trennung umsonst gewesen.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
from pathlib import Path
from typing import Any

import psycopg
import pypdfium2
import pytest
from psycopg_pool import AsyncConnectionPool

from mineru_ocr.config import Settings
from mineru_ocr.db import DocumentStatus, get_document, insert_document
from mineru_ocr.errors import (
    ConfigurationError,
    CorruptDocumentError,
    InsufficientBalanceError,
    TerminalRunPodError,
    TransientRunPodError,
    UnsupportedTypeError,
)
from mineru_ocr.executor import process_document, wait_for_balance
from mineru_ocr.mime import DocumentType

pytestmark = pytest.mark.db


class FalscherRunPod:
    """Ein RunPod-Zugang ohne Netz, der mitschreibt, was er tat.

    Er bildet die drei Methoden nach, die der Executor benutzt, und zaehlt
    dabei mit, WANN abgesendet und WANN abgeholt wurde. Genau daran haengt die
    wichtigste Zusicherung des Executors: dass je Dokument nie zwei Ergebnisse
    gleichzeitig unabgeholt sind.
    """

    def __init__(
        self,
        antworten: list[dict[str, Any]] | None = None,
        *,
        marke: str | None = ":17d4955b2",
        kontostand: float = 5.0,
    ) -> None:
        self.antworten = antworten or []
        self.marke = marke
        self.kontostand = kontostand
        self.abgesendet: list[str] = []
        self.abgeholt: list[str] = []
        self.verlauf: list[str] = []
        self._nummer = 0

    async def submit(
        self, *, file_b64: str, backend: str, transport: str, **_: Any
    ) -> str:
        job = f"job-{len(self.abgesendet)}"
        self.abgesendet.append(file_b64)
        self.verlauf.append(f"submit:{job}")
        return job

    async def await_result(self, job_id: str) -> dict[str, Any]:
        self.abgeholt.append(job_id)
        self.verlauf.append(f"holen:{job_id}")
        nummer = min(self._nummer, len(self.antworten) - 1)
        self._nummer += 1
        return self.antworten[nummer]

    async def image_tag(self) -> str | None:
        return self.marke

    async def balance(self) -> float:
        return self.kontostand


def _fertige_antwort(
    markdown: str = "Text", content_list: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Eine COMPLETED-Antwort in der Form der echten Antworten."""
    return {
        "id": "erfunden",
        "status": "COMPLETED",
        "workerId": "wrk",
        "delayTime": 1,
        "executionTime": 2,
        "output": {
            "ok": True,
            "results": [
                {
                    "basename": "doc",
                    "markdown": markdown,
                    "content_list": content_list
                    if content_list is not None
                    else [{"type": "text", "text": markdown, "page_idx": 0}],
                    "images": {},
                    "middle": {"_backend": "vlm"},
                }
            ],
        },
    }


def _pdf(seiten: int) -> bytes:
    doc = pypdfium2.PdfDocument.new()
    try:
        for _ in range(seiten):
            doc.new_page(200.0, 300.0)
        puffer = io.BytesIO()
        doc.save(puffer)
        return puffer.getvalue()
    finally:
        doc.close()


def _anlegen(
    conn: psycopg.Connection[Any],
    tmp_path: Path,
    inhalt: bytes,
    *,
    name: str = "x.pdf",
    typ: DocumentType = DocumentType.PDF,
    seiten: int | None = None,
) -> tuple[str, Path]:
    """Legt Datei und Zeile an und gibt Hash und Pfad zurueck."""
    pfad = tmp_path / name
    pfad.write_bytes(inhalt)
    sha = hashlib.sha256(inhalt).hexdigest()
    insert_document(
        conn,
        sha256=sha,
        source_path=str(pfad),
        source_name=name,
        size_bytes=len(inhalt),
        mime=typ.mime,
        page_count=seiten,
        endpoint_id="l79lebhinxaqjz",
    )
    return sha, pfad


@pytest.fixture
async def pool(_test_datenbank: str) -> Any:
    """Ein echter Verbindungspool auf die Test-Datenbank.

    Ein echter und kein nachgebauter: Der Executor gibt seine Verbindung
    ausdruecklich zwischen Lesen und Schreiben ZURUECK, und ob er das wirklich
    tut, laesst sich nur an einem Pool mit begrenzter Groesse pruefen.
    """
    p = AsyncConnectionPool(_test_datenbank, min_size=1, max_size=2, open=False)
    await p.open(wait=True)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def exec_settings(settings: Settings) -> Settings:
    return dataclasses.replace(
        settings,
        endpoint_id="l79lebhinxaqjz",
        poll_interval_seconds=0.01,
        balance_recheck_seconds=0.01,
    )


# --- Der Guard ------------------------------------------------------------


async def test_guard_haelt_fertige_dokumente_an(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bei `done`, `empty` und `input_error` kehrt der Ablauf sofort zurueck und
    schickt NICHTS ab — sonst waere ein Wiederlauf nach dem finalen Schreiben ein
    vollstaendig neu bezahltes Dokument."""
    from mineru_ocr.db import write_terminal_async

    for nr, status in enumerate(
        [DocumentStatus.DONE, DocumentStatus.EMPTY, DocumentStatus.INPUT_ERROR]
    ):
        sha, _ = _anlegen(
            db_conn, tmp_path, f"inhalt-{nr}".encode(), name=f"{nr}.png",
            typ=DocumentType.PNG,
        )
        await write_terminal_async(
            async_conn, sha256=sha, status=status, raw={"erste": nr}
        )

        runpod = FalscherRunPod([_fertige_antwort()])
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

        assert runpod.abgesendet == [], status
        zeile = get_document(db_conn, sha)
        assert zeile is not None
        assert zeile.raw == {"erste": nr}, status


async def test_guard_laesst_failed_durch(
    db_conn: psycopg.Connection[Any],
    async_conn: psycopg.AsyncConnection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Bei `failed` wird wirklich neu verarbeitet.

    Der Gegentest zum vorigen, und der wichtigere: Stuende `failed` im Guard,
    waere jeder Retry von Hand ein Nichts-Tun, das als Erfolg verbucht wird — und
    ein gescheitertes Dokument koennte nie wieder laufen.
    """
    from mineru_ocr.db import write_terminal_async

    sha, _ = _anlegen(
        db_conn, tmp_path, b"bild", name="a.png", typ=DocumentType.PNG
    )
    await write_terminal_async(
        async_conn,
        sha256=sha,
        status=DocumentStatus.FAILED,
        raw={"versuch": 1},
        error="damals gescheitert",
    )

    runpod = FalscherRunPod([_fertige_antwort("Jetzt geklappt")])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Jetzt geklappt"


# --- Eingang --------------------------------------------------------------


async def test_veraenderte_datei_wird_erkannt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Weicht der Hash beim Verarbeiten ab, endet das Dokument `input_error` mit
    Vermerk — und es wird nichts abgeschickt."""
    sha, pfad = _anlegen(
        db_conn, tmp_path, b"urspruenglich", name="a.png", typ=DocumentType.PNG
    )
    pfad.write_bytes(b"inzwischen etwas ganz anderes")

    runpod = FalscherRunPod([_fertige_antwort()])
    with pytest.raises(Exception):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    assert runpod.abgesendet == []
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert zeile.error is not None
    assert any(v["kind"] == "gescheitert" for v in zeile.notes)


async def test_nicht_pdf_laeuft_als_ein_teilstueck(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Bild ergibt genau einen Chunk `(0, 0)`; es gibt keinen Sonderfall
    „war nicht geschnitten"."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"bilddaten", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([_fertige_antwort("Bildtext")])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    assert base64.b64decode(runpod.abgesendet[0]) == b"bilddaten"
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


async def test_kleines_pdf_geht_ungeschnitten_hinaus(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein PDF unter beiden Schwellen wird NICHT neu geschrieben.

    Es gehen die ORIGINAL-Bytes hinaus. Ein Ausschnitt waere byte-gleich im
    Inhalt, aber ein neu geschriebenes Dokument — unnoetige Arbeit an genau der
    Stelle, an der jede Umformung ein Risiko ist.
    """
    inhalt = _pdf(3)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt, seiten=3)
    runpod = FalscherRunPod([_fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert len(runpod.abgesendet) == 1
    assert base64.b64decode(runpod.abgesendet[0]) == inhalt


async def test_grosses_pdf_wird_geschnitten(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ueber der Seiten-Schwelle wird zerlegt, und die Teilstuecke decken das
    Dokument vollstaendig ab."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=2)
    inhalt = _pdf(5)
    sha, _ = _anlegen(db_conn, tmp_path, inhalt, seiten=5)
    runpod = FalscherRunPod([_fertige_antwort("Stueck")])
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    # 5 Seiten bei Schwelle 2 → 2 + 2 + 1.
    assert len(runpod.abgesendet) == 3
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE


# --- Absenden und Abholen -------------------------------------------------


async def test_hoechstens_ein_ergebnis_offen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Zu keinem Zeitpunkt sind je Dokument zwei Ergebnisse unabgeholt.

    Die Zusicherung gegen das verpasste Abholfenster: nie absenden-und-spaeter-
    einsammeln. Genau das machten die alten Ad-hoc-Skripte anders.

    Geprueft am VERLAUF: Auf jedes `submit` muss unmittelbar sein `holen` folgen,
    bevor das naechste `submit` kommt. Ein Verlauf wie submit/submit/holen/holen
    waere die alte Bauart und wuerde hier auffallen.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(4), seiten=4)
    runpod = FalscherRunPod([_fertige_antwort()])
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    assert runpod.verlauf == [
        "submit:job-0",
        "holen:job-0",
        "submit:job-1",
        "holen:job-1",
        "submit:job-2",
        "holen:job-2",
        "submit:job-3",
        "holen:job-3",
    ]


@pytest.mark.parametrize(
    "zustand, erwartet",
    [
        ("FAILED", TerminalRunPodError),
        ("CANCELLED", TerminalRunPodError),
        ("TIMED_OUT", TransientRunPodError),
    ],
)
async def test_endzustaende_werden_uebersetzt(
    zustand: str,
    erwartet: type[Exception],
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Nicht-COMPLETED-Endzustaende werden in die richtige Ausnahme uebersetzt.

    `FAILED` und `CANCELLED` sind endgueltig; `TIMED_OUT` ist wiederholbar, denn
    es kann an der Warteschlange gelegen haben. Der Unterschied entscheidet, ob
    das Dokument aufgegeben oder noch zweimal bezahlt wird.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, zustand.encode(), name=f"{zustand}.png",
        typ=DocumentType.PNG,
    )
    runpod = FalscherRunPod([{"id": "j", "status": zustand, "error": "grund"}])
    with pytest.raises(erwartet):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )


async def test_ausgepackt_wird_nur_erfolgreiches(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`unpack_response` sieht ausschliesslich `COMPLETED`-Antworten — die
    Uebersetzung liegt davor.

    Geprueft daran, dass eine FAILED-Antwort OHNE `output` nicht am Auspacken
    scheitert (was ein anderer Fehlertyp waere), sondern an der Uebersetzung.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([{"id": "j", "status": "FAILED"}])
    with pytest.raises(TerminalRunPodError) as fehler:
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )
    assert "FAILED" in str(fehler.value)
    assert "output" not in str(fehler.value).split("Volle Antwort")[0]


# --- Schreiben ------------------------------------------------------------


async def test_genau_ein_schreibvorgang(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Zwischen Anlage und Endzustand wird die Zeile GENAU EINMAL beschrieben.

    Das ist die Zusicherung, an der das ganze Ein-Buch-Modell haengt: kein
    Zwischenstand, kein „laeuft gerade", keine Chunk-Zustaende in der Zeile.

    DIESER TEST HAT SIE NICHT GEPRUEFT. Er verglich `updated_at` vorher und
    nachher und verlangte „groesser" — das ist „MINDESTENS einmal". Zwei, drei
    oder zehn Schreibvorgaenge haetten ihn genauso bestanden, und die
    Begruendung im Docstring („Ein Zwischen-Update haette sie ein zweites Mal
    bewegt") stimmt schlicht nicht: Ein zweites Update bewegt denselben
    Zeitstempel ein zweites Mal weiter, und „weiter" bleibt „weiter".

    Gezaehlt wird jetzt wirklich, mit einem Zaehl-Ausloeser auf der Tabelle. Das
    ist die einzige Art, „genau einmal" zu pruefen, ohne dem Code zu glauben,
    den man prueft.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3), seiten=3)
    vorher = get_document(db_conn, sha)
    assert vorher is not None
    assert vorher.updated_at == vorher.created_at

    # Ein Zaehler, der JEDES UPDATE auf dieser Zeile mitschreibt — unabhaengig
    # davon, welche Spalten es anfasst und ob es den Zeitstempel bewegt.
    db_conn.execute("CREATE TABLE IF NOT EXISTS _schreibzaehler (sha256 text)")
    db_conn.execute("TRUNCATE _schreibzaehler")
    db_conn.execute(
        "CREATE OR REPLACE FUNCTION _zaehle_schreibvorgang() RETURNS trigger AS $$ "
        "BEGIN INSERT INTO _schreibzaehler VALUES (NEW.sha256); RETURN NEW; END; "
        "$$ LANGUAGE plpgsql"
    )
    db_conn.execute(
        "CREATE OR REPLACE TRIGGER _zaehl_trigger AFTER UPDATE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION _zaehle_schreibvorgang()"
    )
    db_conn.commit()
    try:
        runpod = FalscherRunPod([_fertige_antwort()])
        await process_document(sha, settings=eng, runpod=runpod, pool=pool)

        zeile = db_conn.execute(
            "SELECT count(*) FROM _schreibzaehler WHERE sha256 = %s", (sha,)
        ).fetchone()
        assert zeile is not None
        # DREI Teilstuecke sind gelaufen — und trotzdem genau EIN Schreibvorgang.
        assert zeile[0] == 1, f"{zeile[0]} Schreibvorgaenge statt genau einem"
    finally:
        db_conn.execute("DROP TRIGGER IF EXISTS _zaehl_trigger ON documents")
        db_conn.commit()

    nachher = get_document(db_conn, sha)
    assert nachher is not None
    assert nachher.updated_at > vorher.updated_at
    assert nachher.status is DocumentStatus.DONE


async def test_ergebnis_ist_zusammengesetzt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Was geschrieben wird, ist EIN Dokument: Text in Reihenfolge, Seitenzahlen
    des Originals, Bilder vereinigt. Am Ergebnis ist nicht ablesbar, ob
    geschnitten wurde."""
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(3), seiten=3)

    # Drei Teilstuecke, jedes mit teilstueck-lokalem page_idx 0.
    runpod = FalscherRunPod(
        [
            _fertige_antwort("Seite A", [{"type": "text", "text": "A", "page_idx": 0}]),
            _fertige_antwort("Seite B", [{"type": "text", "text": "B", "page_idx": 0}]),
            _fertige_antwort("Seite C", [{"type": "text", "text": "C", "page_idx": 0}]),
        ]
    )
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.raw is not None
    assert zeile.raw["markdown"] == "Seite A\n\nSeite B\n\nSeite C"
    # DIE Korrektur, um die es geht: aus dreimal „Seite 0" werden 0, 1, 2.
    assert [e["page_idx"] for e in zeile.raw["content_list"]] == [0, 1, 2]
    # Und es gibt kein Feld, das die Teilung noch verriete.
    assert "chunks" not in zeile.raw
    assert "chunk_plan" not in zeile.raw


async def test_leeres_ergebnis_wird_empty(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein Dokument ohne jeden Inhalt endet `empty`, nicht `done` und nicht
    `failed`.

    Beide Verwechslungen sind in den Ad-hoc-Skripten je einmal wirklich
    passiert: als Erfolg getarnt ist es unsichtbarer Verlust, als Fehler getarnt
    endlose Wiedervorlage mit Doppelkosten.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"leerbild", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([_fertige_antwort("", [])])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.EMPTY


async def test_image_tag_wird_mitgeschrieben(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`image_tag_at_submit` ist nach dem Lauf gesetzt.

    Ohne diesen Weg bliebe die Spalte fuer immer leer — und die Frage, fuer die
    sie da ist, waere nach Wochen nicht mehr entscheidbar.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([_fertige_antwort()], marke=":abc12345")
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.image_tag_at_submit == ":abc12345"


async def test_unlesbarer_image_tag_kostet_kein_dokument(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Scheitert das Lesen der Herkunft, laeuft das Dokument trotzdem durch.

    Die Angabe ist wertvoll, aber sie ist nicht das Ergebnis. Ein Netzwackeln
    beim Lesen einer Nebenangabe darf keinen bezahlten Parse kosten — der
    Vermerk sagt dann, warum sie fehlt.
    """

    class OhneMarke(FalscherRunPod):
        async def image_tag(self) -> str | None:
            raise TransientRunPodError("GraphQL antwortet nicht")

    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    runpod = OhneMarke([_fertige_antwort()])
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.image_tag_at_submit is None
    assert any(v["kind"] == "image_tag_nicht_lesbar" for v in zeile.notes)


async def test_wiederholbarer_fehler_laesst_die_zeile_unberuehrt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Geworfen, nichts geschrieben — die Warteschlange legt neu vor."""
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )
    vorher = get_document(db_conn, sha)
    assert vorher is not None

    runpod = FalscherRunPod([{"id": "j", "status": "TIMED_OUT"}])
    with pytest.raises(TransientRunPodError):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    nachher = get_document(db_conn, sha)
    assert nachher == vorher


async def test_endgueltiger_fehler_schreibt_vor_dem_werfen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Zeile `failed` samt dem, was bis dahin ankam, DANN die Ausnahme. So tragen
    Job-Endzustand und Zeile dieselbe Wahrheit.

    Und das Bezahlte geht NICHT verloren: Das erste Teilstueck kam durch, das
    zweite scheiterte — im `raw` steht trotzdem, was das erste lieferte.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2), seiten=2)

    runpod = FalscherRunPod(
        [
            _fertige_antwort("Erstes Stueck kam an"),
            {"id": "j", "status": "FAILED", "error": "Modellfehler"},
        ]
    )
    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None
    # Das Bezahlte ist da.
    assert "Erstes Stueck kam an" in zeile.raw["markdown"]
    # Und daneben steht, WORAN es scheiterte — sonst saehe die Zeile aus wie ein
    # unvollstaendiges Ergebnis ohne Grund.
    assert zeile.raw["failure"]["type"] == "TerminalRunPodError"
    assert any(v["kind"] == "gescheitert" for v in zeile.notes)
    assert zeile.notes[-1]["teilstuecke_bezahlt"] == 1


# --- Geld -----------------------------------------------------------------


async def test_leeres_konto_wartet(exec_settings: Settings) -> None:
    """`wait_for_balance` kehrt erst zurueck, wenn der Stand wieder reicht.

    Und es WIRFT nicht: Wuerde es werfen, verbrauchte die Wartezeit die drei
    echten Versuche, und ein Bestand liefe bei kurzzeitig leerem Konto
    reihenweise in `failed`.
    """

    class Aufladend(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.abfragen = 0

        async def balance(self) -> float:
            self.abfragen += 1
            return 0.0 if self.abfragen < 3 else 5.0

    runpod = Aufladend()
    await wait_for_balance(runpod, exec_settings)
    assert runpod.abfragen == 3


async def test_gescheiterte_kontoabfrage_beendet_das_warten_nicht(
    exec_settings: Settings,
) -> None:
    """Ein Netzwackeln ist kein „Konto in Ordnung" — sonst liefe man in eine Wand
    aus Ablehnungen, jede ein bezahlter Fehlversuch."""

    class Wackelnd(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.abfragen = 0

        async def balance(self) -> float:
            self.abfragen += 1
            if self.abfragen < 3:
                raise TransientRunPodError("GraphQL antwortet nicht")
            return 5.0

    runpod = Wackelnd()
    await wait_for_balance(runpod, exec_settings)
    assert runpod.abfragen == 3


async def test_endgueltiger_fehler_der_kontoabfrage_beendet_das_warten(
    exec_settings: Settings,
) -> None:
    """Ein widerrufener Schluessel laesst das Warten NICHT endlos weiterlaufen.

    Der Gegenfall zum Test darueber, und der teurere von beiden: Frueher fing die
    Schleife jede Ausnahme. Ein `ConfigurationError` (HTTP 401/403 — laut
    `errors.py` „immer endgueltig, nie ein Grund fuer einen Retry") liess damit
    JEDES Dokument vor seinem ersten Teilstueck haengen, endlos und ohne jede
    Meldung. Der Lauf stand still und war dabei von „laeuft" nicht zu
    unterscheiden.
    """

    class TotesKonto(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__()
            self.abfragen = 0

        async def balance(self) -> float:
            self.abfragen += 1
            raise ConfigurationError("HTTP 401: Schluessel widerrufen")

    runpod = TotesKonto()
    with pytest.raises(ConfigurationError):
        await wait_for_balance(runpod, exec_settings)
    # EINMAL gefragt, nicht endlos: Der Fehler wird weitergereicht, nicht
    # ausgesessen.
    assert runpod.abfragen == 1


async def test_402_beim_absenden_pausiert_statt_zu_werfen(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Regel 8 am ganzen Ablauf: Ein leeres Konto MITTEN im Lauf pausiert.

    Das Szenario, fuer das die Regel geschrieben wurde: Ein mehrteiliges Dokument
    laeuft, das Konto leert sich zwischen zwei Teilstuecken. Vorher lief der 402
    ungefangen nach oben, wurde als wiederholbar eingestuft und die Warteschlange
    legte den Job neu vor — was zweierlei kostete: Das bereits bezahlte erste
    Teilstueck war verworfen und wurde neu bezahlt, und die Wiedervorlage zehrte
    am Budget aus `max_attempts`.

    Geprueft wird deshalb beides: dass die Ausnahme NICHT nach oben geht, und
    dass das erste Teilstueck erhalten bleibt.
    """
    eng = dataclasses.replace(exec_settings, max_pages_per_chunk=1)
    sha, _ = _anlegen(db_conn, tmp_path, _pdf(2), seiten=2)

    class KontoLeertSich(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__(
                [
                    _fertige_antwort("Erstes Stueck"),
                    _fertige_antwort("Zweites Stueck"),
                ]
            )
            self.leer = True

        async def submit(
            self, *, file_b64: str, backend: str, transport: str, **_: Any
        ) -> str:
            if len(self.abgesendet) == 1 and self.leer:
                # Beim ZWEITEN Absenden ist das Konto leer. Danach ist es
                # aufgeladen — genau so, wie eine Nachzahlung wirkt.
                self.leer = False
                raise InsufficientBalanceError("HTTP 402: Konto leer")
            return await super().submit(
                file_b64=file_b64, backend=backend, transport=transport
            )

    runpod = KontoLeertSich()
    await process_document(sha, settings=eng, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    assert zeile.raw is not None
    # Beide Teilstuecke sind da — das erste wurde durch die Pause nicht verworfen.
    assert "Erstes Stueck" in zeile.raw["markdown"]
    assert "Zweites Stueck" in zeile.raw["markdown"]
    # Und es wurde genau zweimal wirklich abgesendet, nicht dreimal: Die Pause
    # wiederholt das gescheiterte Absenden, sie sendet nichts zusaetzlich.
    assert len(runpod.abgesendet) == 2


async def test_402_beim_abholen_sendet_den_job_nicht_neu_ab(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Ein 402 beim Abholen fuehrt NICHT zu einem zweiten Absenden.

    Der Grund, warum Absenden und Abholen getrennt pausieren: Ist der Job schon
    abgesendet, ist er bezahlt. Ihn nach einer Pause erneut abzuschicken zahlte
    ihn ein zweites Mal UND liesse das erste Ergebnis in seinem
    Dreissig-Minuten-Fenster verfallen — der teuerste denkbare Ausgang einer
    Massnahme, die Geld sparen soll.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"ein bild", name="b.png", typ=DocumentType.PNG
    )

    class LeerBeimAbholen(FalscherRunPod):
        def __init__(self) -> None:
            super().__init__([_fertige_antwort("Fertig geworden")])
            self.versuche = 0

        async def await_result(self, job_id: str) -> dict[str, Any]:
            self.versuche += 1
            if self.versuche == 1:
                raise InsufficientBalanceError("HTTP 402 beim Abfragen")
            return await super().await_result(job_id)

    runpod = LeerBeimAbholen()
    await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.DONE
    # DAS ist die Zusicherung: einmal abgesendet, zweimal abgeholt.
    assert len(runpod.abgesendet) == 1
    assert runpod.versuche == 2


async def test_402_trotz_gedecktem_konto_wird_als_widerspruch_benannt(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Sagt der Kontostand „gedeckt" und der Endpoint trotzdem 402, wird der
    Widerspruch benannt statt ausgesessen.

    Die Pause wartet, so lange das Konto leer ist — das ist gewollt und
    unbegrenzt. Aber `wait_for_balance` kehrt nur zurueck, wenn der Stand ueber
    der Schwelle liegt; kommt DANN erneut ein 402, widersprechen sich zwei
    Quellen. Endlos darauf zu warten hiesse, auf eine Bedingung zu warten, die
    nach der einen Quelle schon erfuellt ist — genau der stille Stillstand, gegen
    den auch die Behandlung endgueltiger Fehler in `wait_for_balance` steht.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"noch ein bild", name="c.png", typ=DocumentType.PNG
    )

    class ImmerLeer(FalscherRunPod):
        """Konto meldet 5,00 USD, der Endpoint lehnt trotzdem jedes Mal ab."""

        async def submit(
            self, *, file_b64: str, backend: str, transport: str, **_: Any
        ) -> str:
            raise InsufficientBalanceError("HTTP 402: Konto leer")

    runpod = ImmerLeer()
    with pytest.raises(TerminalRunPodError) as gefangen:
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    assert "widersprechen einander" in str(gefangen.value)
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED


async def test_fehlerpfad_schreibt_auch_wenn_das_zusammensetzen_scheitert(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Der Endzustand wird auch dann geschrieben, wenn `assemble` SELBST wirft.

    Der teuerste Fehler, den dieses Modul hatte: Der Fehlerpfad baute das zu
    schreibende `raw` mit demselben `assembly.assemble`, das im Erfolgspfad
    geworfen haben kann. War der urspruengliche Fehler genau dieser Wurf, warf
    der Aufruf im Fehlerpfad sofort wieder, die Ausnahme verliess den Handler,
    und geschrieben wurde NIE.

    Die Folge war nicht nur der Verlust der bezahlten Teilstuecke: Der Job endete
    `failed`, die Zeile blieb `new`, das Dokument zaehlte fuer immer als
    `pending` — und weil ein Ordner-Durchlauf genau die `pending` anfasst,
    bezahlte JEDER weitere Durchlauf das ganze Dokument neu und scheiterte
    deterministisch wieder an derselben Stelle.

    Ausgeloest wird der Wurf hier ueber ein `page_idx`, das keine ganze Zahl ist
    — `assemble` kann den Seitenversatz dann nicht berechnen und wirft, wie es
    soll.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"kaputte antwort", name="d.png", typ=DocumentType.PNG
    )

    runpod = FalscherRunPod(
        [
            _fertige_antwort(
                "Bezahlt und angekommen",
                content_list=[{"type": "text", "text": "x", "page_idx": 1.5}],
            )
        ]
    )
    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    # ZUERST geschrieben, DANN geworfen — auch hier.
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None
    # Das Bezahlte ist in der Notfassung erhalten, unbearbeitet.
    assert "assembly_failed" in zeile.raw
    assert zeile.raw["chunks"][0]["markdown"] == "Bezahlt und angekommen"
    assert zeile.raw["chunks"][0]["content_list"][0]["page_idx"] == 1.5
    # Und der Leser sieht, dass er die Notfassung vor sich hat.
    assert any(v["kind"] == "zusammensetzen_gescheitert" for v in zeile.notes)


async def test_bezahlte_antwort_mit_formfehler_wird_aufgehoben(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Eine BEZAHLTE `COMPLETED`-Antwort, die sich nicht auspacken laesst, landet
    trotzdem vollstaendig in `raw`.

    Vorher ging sie verloren: `unpack_response` warf bei einer Formabweichung,
    die Ausnahme trug nur die Schluesselnamen in ihrer Meldung, und die Antwort
    selbst wanderte weder in `teile` noch in `raw`. Der Job war zu diesem
    Zeitpunkt schon bezahlt, und dreissig Minuten spaeter ist eine Antwort auf
    der Gegenseite nicht mehr abrufbar — bei jeder kuenftigen Fassung der Engine,
    die ein Feld anders benennt, waere das Ergebnis also unwiederbringlich weg
    gewesen. Genau der Verlust, gegen den die Abruf-Regel steht: alles holen,
    alles persistieren, nie filtern.

    Hier antwortet die Gegenseite mit `COMPLETED`, aber mit einer `output`-Form,
    die es so nicht geben duerfte — zwei Ergebnisse statt einem.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"formfehler", name="e.png", typ=DocumentType.PNG
    )

    kaputte_antwort = {
        "id": "job-formfehler",
        "status": "COMPLETED",
        "workerId": "wrk-7",
        "executionTime": 4242,
        "output": {
            "ok": True,
            "results": [
                {"markdown": "Erstes Ergebnis", "content_list": []},
                {"markdown": "Zweites Ergebnis", "content_list": []},
            ],
        },
    }
    runpod = FalscherRunPod([kaputte_antwort])

    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None

    # DIE ZUSICHERUNG: die ganze Antwort steht da, nicht nur eine Fehlermeldung
    # ueber sie.
    aufgehoben = zeile.raw["unverwertbare_antworten"]
    assert len(aufgehoben) == 1
    assert aufgehoben[0]["antwort"] == kaputte_antwort
    assert aufgehoben[0]["job_id"] == "job-0"
    assert aufgehoben[0]["chunk"] == {"first_page": 0, "last_page": 0}
    # Und der Grund steht daneben, sonst saehe die Zeile aus wie ein
    # unvollstaendiges Ergebnis ohne Anlass.
    assert "TerminalRunPodError" in aufgehoben[0]["fehler"]


async def test_echte_fehlerantwort_landet_vollstaendig_in_raw(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
    echte_fehlerantwort: dict[str, Any],
) -> None:
    """Eine ECHTE `FAILED`-Antwort steht danach als abfragbares JSON in `raw`.

    Erzwungen am 09.08.2026 gegen den echten Endpoint (gueltiges PDF,
    ungueltiger Backend-Name) und als Fixture konserviert. Sie zeigt, dass eine
    Fehlerantwort weit mehr traegt als „es ging schief": `error` auf oberster
    Ebene, ein volles `output` mit `ok: false`, den `debug`-Block mit GPU,
    Modellverzeichnis und Phasenzeiten — und einen kompletten `traceback` aus
    dem Worker.

    Vorher stand `_endzustand_uebersetzen` VOR dem Schutzblock, der bezahlte
    Antworten aufhebt. Der Traceback ueberlebte damit nur als Python-
    Textdarstellung, eingebettet in die Fehlermeldung — vorhanden, aber weder
    abfragbar noch zuverlaessig lesbar. Fuer die beste Diagnose, die es zu einem
    gescheiterten Dokument gibt, ist das zu wenig.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"loest fehler aus", name="f.png", typ=DocumentType.PNG
    )
    runpod = FalscherRunPod([echte_fehlerantwort])

    with pytest.raises(TerminalRunPodError):
        await process_document(sha, settings=exec_settings, runpod=runpod, pool=pool)

    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.FAILED
    assert zeile.raw is not None

    aufgehoben = zeile.raw["unverwertbare_antworten"]
    assert len(aufgehoben) == 1
    # Die GANZE Antwort, unveraendert.
    assert aufgehoben[0]["antwort"] == echte_fehlerantwort
    # Und damit insbesondere das, wofuer man sie aufhebt.
    ausgabe = aufgehoben[0]["antwort"]["output"]
    assert ausgabe["ok"] is False
    assert "Traceback" in ausgabe["traceback"]
    assert ausgabe["debug"]["gpu"]["name"].startswith("NVIDIA")


async def test_eingabeprobleme_enden_input_error_nicht_failed(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Was VOR dem ersten bezahlten Aufruf schiefgeht, ist `input_error`.

    `db.py` trennt die beiden Endzustaende genau daran: `failed` beantwortet
    „wofuer haben wir bezahlt?", `input_error` sagt „mit dieser Eingabe konnten
    wir nichts anfangen". Zwei Faelle landeten trotzdem auf `failed`, beide
    ueber die Regel „Unbekanntes ist endgueltig":

        eine MIME-Zeichenkette, zu der es keinen bekannten Typ gibt,
        und eine Quelldatei, die zwischen Einreihen und Verarbeiten
        verschwindet.

    Bezahlt ist in beiden Faellen nichts. Und der Unterschied ist nicht bloss
    Etikett: `input_error` steht im Terminal-Guard und wird nicht wieder
    angefasst, `failed` ausdruecklich schon — ein Retry von Hand liesse ein
    Dokument mit unbekanntem MIME-Typ also erneut anlaufen, ohne dass sich
    daran etwas aendern koennte.
    """
    # 1. Unbekannte MIME-Zeichenkette in der Zeile.
    sha_mime, _ = _anlegen(
        db_conn, tmp_path, b"inhalt", name="m.png", typ=DocumentType.PNG
    )
    db_conn.execute(
        "UPDATE documents SET mime = %s WHERE sha256 = %s",
        ("application/x-gibt-es-nicht", sha_mime),
    )
    runpod = FalscherRunPod([_fertige_antwort()])
    with pytest.raises(UnsupportedTypeError):
        await process_document(
            sha_mime, settings=exec_settings, runpod=runpod, pool=pool
        )
    zeile = get_document(db_conn, sha_mime)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    # Und es wurde NICHTS abgesendet.
    assert runpod.abgesendet == []

    # 2. Quelldatei zwischen Einreihen und Verarbeiten geloescht.
    sha_weg, pfad = _anlegen(
        db_conn, tmp_path, b"gleich weg", name="w.png", typ=DocumentType.PNG
    )
    pfad.unlink()
    runpod2 = FalscherRunPod([_fertige_antwort()])
    with pytest.raises(CorruptDocumentError):
        await process_document(
            sha_weg, settings=exec_settings, runpod=runpod2, pool=pool
        )
    zeile = get_document(db_conn, sha_weg)
    assert zeile is not None
    assert zeile.status is DocumentStatus.INPUT_ERROR
    assert runpod2.abgesendet == []


def test_ungueltiges_backend_faellt_lokal_auf(settings: Settings) -> None:
    """Ein Tippfehler im Backend-Namen kostet keinen Kaltstart mehr.

    Die gueltigen Namen sind von der Gegenseite ABGELESEN: Am 09.08.2026 wurde
    absichtlich ein ungueltiger geschickt, und die Fehlerantwort nannte die
    Liste selbst. Bis dahin war ein Tippfehler nur zu erfahren, indem man dafuer
    bezahlte — im gemessenen Fall 177 Sekunden Wartezeit bis zur Meldung, und
    das je Dokument. Ueber einen Bestandslauf waere es derselbe Fehler 30 000
    Mal.
    """
    import dataclasses

    from mineru_ocr.config import GUELTIGE_BACKENDS
    from mineru_ocr.errors import ConfigurationError

    # Die Voreinstellung ist gueltig.
    assert settings.backend in GUELTIGE_BACKENDS

    with pytest.raises(ConfigurationError, match="kennt der Worker nicht"):
        dataclasses.replace(settings, backend="vlm-auto-engien")

    # Und jeder von der Gegenseite genannte Name geht durch.
    for name in GUELTIGE_BACKENDS:
        assert dataclasses.replace(settings, backend=name).backend == name


# --- Verbindungen und Abbruch ---------------------------------------------


async def test_keine_verbindung_waehrend_des_wartens(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """Waehrend der Abfrageschleife haelt der Ablauf keine Datenbankverbindung.

    Sonst blockierten vierundzwanzig gleichzeitig betreute Dokumente den Pool bis
    zu fuenfundzwanzig Minuten, waehrend keines von ihnen ihn benutzt.

    Geprueft mit einem Pool der Groesse EINS: Waehrend das Dokument auf RunPod
    wartet, muss eine ZWEITE Abfrage durch denselben Pool gehen koennen. Haelt
    der Executor seine Verbindung, laeuft dieser Test in den Zeitablauf.
    """
    kleiner = AsyncConnectionPool(
        exec_settings.db_dsn, min_size=1, max_size=1, open=False
    )
    await kleiner.open(wait=True)
    try:
        sha, _ = _anlegen(
            db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
        )

        durchgekommen = asyncio.Event()

        class Wartend(FalscherRunPod):
            async def await_result(self, job_id: str) -> dict[str, Any]:
                # Genau HIER — mitten im Warten auf RunPod — muss der Pool frei
                # sein.
                async with kleiner.connection() as zweite:
                    await zweite.execute("SELECT 1")
                durchgekommen.set()
                return await super().await_result(job_id)

        runpod = Wartend([_fertige_antwort()])
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=kleiner
        )
        assert durchgekommen.is_set()
    finally:
        await kleiner.close()


async def test_abbruch_wird_durchgereicht(
    db_conn: psycopg.Connection[Any],
    pool: AsyncConnectionPool,
    exec_settings: Settings,
    tmp_path: Path,
) -> None:
    """`CancelledError` wird nicht gefangen: Der Job endet `aborted`, die Zeile
    bleibt `new`, das Dokument laeuft beim naechsten Einreihen von vorn.

    Der Test faengt einen echten Fehler ab, den man leicht macht: Waere hier
    `except BaseException` statt `except Exception`, wuerde der Abbruch als
    endgueltiger Fehler verbucht — und jedes Strg-C machte reihenweise `failed`.
    """
    sha, _ = _anlegen(
        db_conn, tmp_path, b"x", name="a.png", typ=DocumentType.PNG
    )

    class Abbrechend(FalscherRunPod):
        async def await_result(self, job_id: str) -> dict[str, Any]:
            raise asyncio.CancelledError()

    runpod = Abbrechend([_fertige_antwort()])
    with pytest.raises(asyncio.CancelledError):
        await process_document(
            sha, settings=exec_settings, runpod=runpod, pool=pool
        )

    # Die Zeile ist unberuehrt — kein `failed`.
    zeile = get_document(db_conn, sha)
    assert zeile is not None
    assert zeile.status is DocumentStatus.NEW
