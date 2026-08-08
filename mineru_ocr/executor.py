"""Die eigentliche Arbeit an EINEM Dokument — ohne jedes Wissen ueber Warteschlangen.

Dieses Modul weiss nichts von Procrastinate. Das ist kein Zufall und keine
Reinheitsuebung, sondern hat zwei handfeste Gruende. Erstens laesst sich der
gesamte Ablauf — lesen, zerlegen, absenden, abholen, schreiben — ohne laufende
Warteschlange durchspielen; alles Heikle daran ist dann in einem gewoehnlichen
Test greifbar. Zweitens bleibt die Warteschlange austauschbar: `tasks.py` ist ein
duenner Adapter darueber, und ein Adapter ist billig zu ersetzen, ein
durchwachsener Ablauf nicht.

Der Ablauf ist der aus Plan 5.1, und das Entscheidende daran ist, was er NICHT
tut: Er fasst die Zeile zwischen Anfang und Ende nicht an. Kein Zwischenstand,
kein „laeuft gerade", keine Chunk-Zustaende. Alles Innere ist innen — „ES KOMEMN
KEINE CHUNKS IM POSTGRES AN DU KEK Der EXEKUTOR MANAGED DOCH ALLES UND SPEICHERT
NUR FERTIG" (Plan 2.4).

Und was er bei einem Absturz tut: nichts Besonderes. Bricht die Verarbeitung
mittendrin ab, wird das Dokument beim naechsten Anlauf VON VORN verarbeitet,
einschliesslich erneuter Bezahlung bereits gelaufener Chunks. Das ist eine
bewusste Entscheidung des Auftraggebers gegen jede Zwischenspeicherung von
Chunks — „darüber machst du dir jetzt gedanken um einen edgecase in einem
edgecase? wtf alter es wird dann ncohmal und das wars wenns nicht fertig ist ists
gefailed" (Plan 2.4). Die Einfachheit des Datenmodells ist mehr wert als die
gesparten Cent eines seltenen Falls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from . import assembly, chunking, db
from .config import Settings
from .errors import (
    InputError,
    InsufficientBalanceError,
    SourceChangedError,
    TerminalRunPodError,
    TransientRunPodError,
    should_run_again,
)
from .mime import DocumentType
from .pdf import PdfReader
from .runpod import RunPodAccess


def _jetzt() -> str:
    """Zeitstempel fuer Vermerke — ISO 8601 mit Zeitzone.

    Mit Zeitzone und nicht ohne: Ein Vermerk ohne Zonenangabe ist beim Lesen
    Wochen spaeter nicht mehr eindeutig, und `notes` wird genau dann gelesen,
    wenn etwas schieflief.
    """
    return datetime.now(UTC).isoformat()


def _fehler_umschlag(fehler: BaseException) -> dict[str, Any]:
    """Der Fehler-Umschlag, der bei `failed` mit in `raw` wandert.

    Warum ueberhaupt einer: `raw` traegt bei einem gescheiterten Dokument, was
    bis dahin ankam — und das ist BEZAHLT. Ohne einen Vermerk daneben, WORAN es
    scheiterte, sieht eine solche Zeile aus wie ein unvollstaendiges Ergebnis
    ohne Grund.
    """
    return {
        "type": type(fehler).__name__,
        "message": str(fehler),
        "at": _jetzt(),
    }


async def process_document(
    sha256: str,
    *,
    settings: Settings,
    runpod: RunPodAccess,
    pool: AsyncConnectionPool,
) -> None:
    """Verarbeitet ein Dokument vollstaendig und schreibt genau einmal sein Ergebnis.

    Der Ablauf in der Reihenfolge, in der er stattfindet:

    0. TERMINAL-GUARD. Steht die Zeile bereits auf `done`, `empty` oder
       `input_error`, kehrt die Funktion sofort zurueck und fasst nichts an. Das
       ist die Idempotenz, die eine Warteschlange voraussetzt — Tasks KOENNEN
       mehrfach laufen (Wiederholungen, stalled-Wiederaufnahme; belegt ueber die
       Mechanik, nicht als Doku-Zitat, Feature-Verifikation Punkt 4). Ohne den
       Guard wuerde ein Wiederlauf nach dem finalen Schreiben — etwa nach einem
       Absturz im Fenster zwischen Zeilen-Schreiben und Job-Verbuchung — das
       fertige Dokument vollstaendig neu bezahlen.

       `failed` gehoert AUSDRUECKLICH NICHT in den Guard. Ein per
       `retry_job_by_id` wieder vorgelegter Job muss das Dokument wirklich neu
       verarbeiten; stuende `failed` im Guard, waere jeder Retry von Hand ein
       Nichts-Tun, das als Erfolg verbucht wuerde, und ein gescheitertes Dokument
       koennte nie wieder laufen.

    1. Zeile laden. Ab hier wird sie bis zum Terminal-Schreiben NICHT mehr
       angefasst.

    2. Datei von `source_path` lesen und den Hash gegenpruefen. Weicht er ab, ist
       die Datei zwischen Einreihen und Verarbeiten ausgetauscht worden:
       `input_error` samt Vermerk. Bezahlte Ergebnisse gehen dabei nicht
       verloren, weil es an dieser Zeile noch keine gibt.

    3. Zerlegen, absenden, abholen — alles nur im Arbeitsspeicher. Fuer PDF ueber
       der Schwelle: `chunking.plan_chunks`, dann `chunking.iter_chunks` mit
       `PdfReader.extract` als Schneider. Fuer alles andere ein einziges
       Teilstueck `Chunk(0, 0)`, das die ganze Datei traegt — es gibt keinen
       Sonderfall „war nicht geschnitten".

       Je Teilstueck NACHEINANDER: absenden, abfragen bis terminal, SOFORT
       abholen und auspacken (`assembly.unpack_response`), dann das naechste.

       3a. ABHOLEN, BEVOR ETWAS ANDERES GESCHIEHT. Ein fertiges Ergebnis liegt
           auf der Gegenseite nur rund dreissig Minuten bereit; danach antwortet
           `/status` mit 404 und die bezahlte Arbeit ist weg, bevor sie je bei
           uns war. Daraus folgt die Bauart, und sie ist eine ZUSICHERUNG, keine
           Absicht: Es wird nie erst alles abgesendet und spaeter eingesammelt.
           Zu jedem Zeitpunkt ist je Dokument hoechstens EIN Ergebnis offen, und
           es wird innerhalb eines Abfrageintervalls abgeholt. Ueber den ganzen
           Daemon sind damit hoechstens `settings.ocr_concurrency` Ergebnisse
           gleichzeitig unterwegs — bei einem Abfrageintervall von zehn Sekunden
           gegen ein Fenster von dreissig Minuten ist der Abstand so gross, dass
           der Fall nicht eintreten kann, solange der Prozess lebt. (Genau das
           machten die alten Ad-hoc-Skripte anders: 120 gleichzeitig offene Jobs,
           eingesammelt in einer zweiten Phase.)

       3b. ENDZUSTAND UEBERSETZEN. `await_result` gibt auch `FAILED`,
           `CANCELLED` und `TIMED_OUT` zurueck, ohne zu werfen — das ist Absicht,
           damit die bezahlte Fehlerantwort nicht verlorengeht. Hier wird sie
           bewertet: alles ausser `COMPLETED` wird in `TerminalRunPodError` bzw.
           `TransientRunPodError` uebersetzt und geworfen. `TIMED_OUT` ist
           wiederholbar (es kann an der Warteschlange gelegen haben), `FAILED`
           und `CANCELLED` sind endgueltig. Erst danach sieht
           `assembly.unpack_response` die Antwort, und es sieht ausschliesslich
           erfolgreiche.

       Nacheinander und nicht gleichzeitig, weil die Parallelitaet an einer
       anderen Stelle wohnt: Der Daemon betreut vierundzwanzig DOKUMENTE
       gleichzeitig, und die eigentliche Nebenlaeufigkeit liegt ohnehin
       serverseitig in der Warteschlange von RunPod. Zwei Regler waeren einer zu
       viel — und Teilstuecke eines Dokuments gleichzeitig loszuschicken hiesse,
       mehrere bezahlte Ergebnisse gleichzeitig zu parken, gegen 3a.

    4. ZUSAMMENSETZEN UND SCHREIBEN — ein Vorgang, ein Schreibzugriff.

       Erst wird nachgerechnet: `chunking.verify_page_coverage` ueber die
       tatsaechlich gelieferten Teilstuecke gegen `page_count`. Das ist die laute
       Gegenrechnung, die der Plan verlangt (fail-loud); bei Abweichung
       `input_error`. Sie steht hier und nicht in `plan_chunks`, weil erst hier
       feststeht, was wirklich geliefert wurde — `iter_chunks` darf unterwegs
       halbieren.

       Dann `assembly.assemble`: Text in Dokumentreihenfolge, Strukturliste mit
       den Seitenzahlen DES ORIGINALS, Bilder vereinigt, alles Uebrige mit. Das
       Ergebnis ist EIN Dokument — am Ergebnis ist nicht mehr ablesbar, ob und wo
       geschnitten wurde, und genau so ist es gewollt: Rein geht das ganze
       Dokument, raus kommt das ganze Dokument.

       Dass hier zusammengesetzt wird und nicht spaeter beim Lesen, ist keine
       Geschmacksfrage: NUR an dieser Stelle ist bekannt, welches Teilstueck
       welche Seiten trug. Wer es auf die Leseseite verschiebt, muss dieses
       Wissen in die Datenbank schreiben — also Chunk-Angaben fuehren, gegen die
       Festlegung.

       Dann der eine Schreibvorgang: `raw` bekommt das zusammengesetzte Ergebnis,
       der Zustand wird `done` oder `empty` (`assembly.is_empty`), das einmal je
       Lauf gelesene Worker-Image geht als `image_tag_at_submit` mit, Messwerte
       und Hinweise nach `notes`, und das Wecksignal geht raus.

    Fehler laufen IMMER ueber den Ausnahme-Pfad, und der ist zweigeteilt:
    Wiederholbare Fehler werden GEWORFEN, ohne die Zeile anzufassen — die
    Warteschlange legt den Job erneut vor, das Dokument laeuft von vorn.
    Endgueltige Fehler schreiben ZUERST (`raw` mit dem, was bis dahin ankam, samt
    Fehler-Umschlag, Zustand `failed`, Wecksignal) und werfen DANN. Diese
    Reihenfolge ist der Grund, warum Job-Endzustand und Zeile immer dieselbe
    Wahrheit tragen.

    Was ein wiederholbarer Fehler mitten im Dokument KOSTET, steht hier, weil es
    sonst niemand sieht: Die bis dahin abgeholten Antworten liegen im
    Arbeitsspeicher dieses Aufrufs und sind mit ihm weg. Das Dokument laeuft von
    vorn, einschliesslich erneuter Bezahlung der schon gelaufenen Teilstuecke. Bei
    zehn Teilstuecken und drei Versuchen sind das im schlechtesten Fall
    siebenundzwanzig zusaetzlich bezahlte Durchlaeufe fuer ein Dokument. Das ist
    die Kehrseite der Festlegung gegen jede Zwischenspeicherung — „es wird dann
    ncohmal und das wars wenns nicht fertig ist ists gefailed" (Plan 2.4) — und
    sie ist bewusst so gewaehlt: Die Einfachheit des Datenmodells ist mehr wert
    als die gesparten Cent eines seltenen Falls.

    Leeres Konto ist keins von beidem: Der Task geht dann selbst in eine
    Warteschleife (`wait_for_balance`), statt zu werfen — sonst verbrauchte
    jedes Warten einen der drei echten Versuche.

    Args:
        sha256: Welches Dokument. Der Job traegt nichts weiter als diesen Hash;
            alles andere steht in der Zeile.
        settings: Schwellen, Zeitschranken, Job-Contract-Werte.
        runpod: Der geteilte Zugang. Gehoert dem Daemon, wird hier nur benutzt.
        pool: Verbindungen zur eigenen Datenbank.

    Returns:
        Nichts. Das Ergebnis steht in der Zeile — das ist der ganze Punkt dieser
        Architektur: Nicht der Rueckgabewert traegt das Ergebnis, sondern die
        Datenbank, und deshalb ueberlebt es jeden Absturz.

    Verbindungen:
        Eine Verbindung wird ZWEIMAL kurz genommen — am Anfang zum Lesen, am Ende
        zum Schreiben — und dazwischen ZURUECKGEGEBEN. Waehrend des Wartens auf
        RunPod haelt diese Funktion KEINE Verbindung. Das ist keine Feinheit: Ein
        Dokument haengt bis zu fuenfundzwanzig Minuten in seiner Abfrageschleife,
        und vierundzwanzig gleichzeitig gehaltene Verbindungen ueber diese Zeit
        wuerden den Verbindungspool blockieren, waehrend niemand ihn benutzt.

    Idempotenz:
        Ja, ueber den Terminal-Guard — mit der ausdruecklichen und gewollten
        Ausnahme von `failed`.

    Nebenlaeufigkeit:
        Vierundzwanzig Aufrufe gleichzeitig sind der Normalbetrieb, aber nie zwei
        fuer DENSELBEN Hash: Dafuer sorgt der `lock` beim Einreihen. Sollte es
        doch geschehen, faengt es der Guard beziehungsweise die Zeilensperre in
        `write_terminal_async`.

    Raises:
        TransientRunPodError: Wiederholbar. Die Zeile bleibt unberuehrt.
        TerminalRunPodError: Endgueltig. Die Zeile steht bereits auf `failed`,
            wenn dies geworfen wird.
        InputError: Endgueltig ohne RunPod-Kontakt. Die Zeile steht bereits auf
            `input_error`.
        asyncio.CancelledError: Der Daemon faehrt herunter. Wird NICHT gefangen.
            Der Job endet dann `aborted`, die Zeile bleibt `new`, und das
            Dokument laeuft beim naechsten Einreihen von vorn. Es gibt fuer
            `aborted` ausdruecklich kein Retry-Bordmittel (Feature-Verifikation
            Punkt 19) — das erneute Einreihen IST der Heilungsweg.

    Kosten: JA. Diese Funktion ist die einzige im Paket, die planmaessig Geld
    ausgibt. Ein Dokument mit N Chunks kostet N Parses; ein Wiederanlauf nach
    einem Absturz kostet sie erneut.
    """
    # --- 0. und 1.: Zeile laden, Terminal-Guard --------------------------
    # Die Verbindung wird SOFORT wieder zurueckgegeben. Alles Weitere geschieht
    # ohne Verbindung — ein Dokument haengt bis zu fuenfundzwanzig Minuten in
    # seiner Abfrageschleife, und vierundzwanzig gehaltene Verbindungen ueber
    # diese Zeit blockierten den Pool, waehrend niemand ihn benutzt.
    async with pool.connection() as conn:
        zeile = await db.get_document_async(conn, sha256)

    if zeile.status in _GUARD_ZUSTAENDE:
        return

    teile: list[assembly.ChunkArtifacts] = []
    vermerke: list[dict[str, Any]] = []
    image_tag: str | None = None
    begonnen = time.monotonic()

    try:
        # --- 2.: Datei lesen und den Hash gegenpruefen -------------------
        pfad = Path(zeile.source_path)
        rohbytes = pfad.read_bytes()
        tatsaechlich = hashlib.sha256(rohbytes).hexdigest()
        if tatsaechlich != sha256:
            raise SourceChangedError(
                f"Die Datei unter {zeile.source_path} traegt jetzt den Hash "
                f"{tatsaechlich}, eingereiht wurde {sha256}. Sie ist zwischen "
                f"Einreihen und Verarbeiten ausgetauscht worden. Es wird NICHT "
                f"die neue Datei verarbeitet: Die Zeile gehoert zur alten, und "
                f"das Ergebnis der neuen unter der alten Kennung abzulegen waere "
                f"eine stille Verwechslung."
            )

        typ = DocumentType(_typ_aus_mime(zeile.mime))

        # Die Herkunft einmal je Lauf. Ein Fehlschlag hier darf KEIN Dokument
        # kosten: Die Angabe ist wertvoll, aber sie ist nicht das Ergebnis. Sie
        # bleibt dann leer, und der Vermerk sagt, warum — ehrlicher als ein
        # erfundener Wert und billiger als ein verlorener Parse.
        try:
            image_tag = await runpod.image_tag()
        except Exception as fehler:  # noqa: BLE001 — bewusst breit, siehe oben
            vermerke.append(
                {
                    "kind": "image_tag_nicht_lesbar",
                    "at": _jetzt(),
                    "detail": f"{type(fehler).__name__}: {fehler}",
                }
            )

        # Vorsorglich, VOR dem ersten bezahlten Aufruf. Bei leerem Konto liefe
        # man sonst in eine Wand aus 402-Antworten.
        await wait_for_balance(runpod, settings)

        # --- 3.: zerlegen, absenden, abholen ----------------------------
        if typ.is_pdf:
            with PdfReader(pfad) as leser:
                seiten = leser.page_count
                if chunking.needs_chunking(
                    size_bytes=len(rohbytes),
                    page_count=seiten,
                    doc_type=typ,
                    settings=settings,
                ):
                    stuecke: Any = chunking.iter_chunks(
                        chunking.plan_chunks(seiten, settings),
                        leser.extract,
                        settings,
                    )
                else:
                    # Passt ungeteilt: Dann gehen die ORIGINAL-Bytes hinaus,
                    # nicht ein neu geschriebener Ausschnitt. Der Chunk deckt
                    # trotzdem den ganzen Seitenbereich ab, damit die
                    # Gegenrechnung in Schritt 4 aufgeht — es gibt keinen
                    # Sonderfall „war nicht geschnitten".
                    stuecke = [
                        (chunking.Chunk(first_page=0, last_page=seiten - 1), rohbytes)
                    ]
                await _stuecke_verarbeiten(
                    stuecke,
                    settings=settings,
                    runpod=runpod,
                    teile=teile,
                )
        else:
            seiten = None
            # Nicht zerlegbar — also muss es ungeteilt passen.
            chunking.check_fits_unsplit(
                size_bytes=len(rohbytes), doc_type=typ, settings=settings
            )
            await _stuecke_verarbeiten(
                [(chunking.Chunk(first_page=0, last_page=0), rohbytes)],
                settings=settings,
                runpod=runpod,
                teile=teile,
            )

        # --- 4.: nachrechnen, zusammensetzen, EINMAL schreiben ----------
        if seiten is not None:
            # Die laute Gegenrechnung. Sie steht hier und nicht in `plan_chunks`,
            # weil erst jetzt feststeht, was wirklich geliefert wurde —
            # `iter_chunks` darf unterwegs halbieren.
            chunking.verify_page_coverage([teil.chunk for teil in teile], seiten)

        ergebnis = assembly.assemble(teile)
        status = (
            db.DocumentStatus.EMPTY
            if assembly.is_empty(ergebnis)
            else db.DocumentStatus.DONE
        )
        vermerke.append(
            {
                "kind": "verarbeitet",
                "at": _jetzt(),
                "teilstuecke": len(teile),
                "sekunden": round(time.monotonic() - begonnen, 1),
            }
        )

        async with pool.connection() as conn:
            await db.write_terminal_async(
                conn,
                sha256=sha256,
                status=status,
                raw=ergebnis,
                image_tag_at_submit=image_tag,
                notes=vermerke,
            )

    except Exception as fehler:
        # `asyncio.CancelledError` erbt von `BaseException` und wird hier
        # ausdruecklich NICHT gefangen: Beim Herunterfahren soll der Job
        # `aborted` enden und die Zeile `new` bleiben. Sie hier als endgueltigen
        # Fehler zu schreiben, machte aus jedem Strg-C reihenweise `failed`.
        if should_run_again(fehler):
            # Wiederholbar: WERFEN, ohne die Zeile anzufassen. Die Warteschlange
            # legt den Job erneut vor, das Dokument laeuft von vorn.
            #
            # `should_run_again` und nicht die Einstufung unmittelbar: Diese
            # Stelle und die Wiederholungs-Strategie in `tasks.py` MUESSEN
            # dieselbe Gruppierung benutzen, sonst laeuft ein Job noch einmal,
            # waehrend die Zeile schon `failed` sagt — oder umgekehrt. Die
            # Begruendung samt des Falls, an dem es schon einmal auseinanderlief,
            # steht an `errors.should_run_again`.
            raise

        # Endgueltig: ZUERST schreiben, DANN werfen. Diese Reihenfolge ist der
        # Grund, warum Job-Endzustand und Zeile immer dieselbe Wahrheit tragen.
        ergebnis = assembly.assemble(teile)
        ergebnis["failure"] = _fehler_umschlag(fehler)
        vermerke.append(
            {
                "kind": "gescheitert",
                "at": _jetzt(),
                "teilstuecke_bezahlt": len(teile),
                "detail": f"{type(fehler).__name__}: {fehler}",
            }
        )
        status = (
            db.DocumentStatus.INPUT_ERROR
            if isinstance(fehler, InputError)
            else db.DocumentStatus.FAILED
        )
        async with pool.connection() as conn:
            await db.write_terminal_async(
                conn,
                sha256=sha256,
                status=status,
                raw=ergebnis,
                image_tag_at_submit=image_tag,
                error=f"{type(fehler).__name__}: {fehler}",
                notes=vermerke,
            )
        raise


_GUARD_ZUSTAENDE: frozenset[db.DocumentStatus] = frozenset(
    {
        db.DocumentStatus.DONE,
        db.DocumentStatus.EMPTY,
        db.DocumentStatus.INPUT_ERROR,
    }
)
"""Die Zustaende, bei denen der Executor sofort zurueckkehrt.

`FAILED` steht hier AUSDRUECKLICH NICHT. Ein per `retry_job_by_id` wieder
vorgelegter Job muss das Dokument wirklich neu verarbeiten; stuende `failed`
hier, waere jeder Retry von Hand ein Nichts-Tun, das als Erfolg verbucht wuerde,
und ein gescheitertes Dokument koennte nie wieder laufen (Feature-Verifikation
Punkt 19, Konsequenz a)."""


def _typ_aus_mime(mime: str) -> str:
    """Findet den Typ zur gespeicherten MIME-Zeichenkette.

    Die Zeile speichert die MIME-Zeichenkette und nicht den Typnamen — sie ist
    das, was ein Mensch beim Nachsehen erkennt. Zurueck kommt man ueber die
    Tabelle in `mime.py`, die dafuer die einzige Quelle ist.
    """
    for typ in DocumentType:
        if typ.mime == mime:
            return typ.value
    raise TerminalRunPodError(
        f"Die Zeile traegt die MIME-Zeichenkette {mime!r}, zu der es keinen "
        f"bekannten Typ gibt. Entweder wurde die Zeile von einer aelteren "
        f"Fassung angelegt, oder die Tabelle in mime.py hat sich geaendert."
    )


async def _stuecke_verarbeiten(
    stuecke: Any,
    *,
    settings: Settings,
    runpod: RunPodAccess,
    teile: list[assembly.ChunkArtifacts],
) -> None:
    """Schickt Teilstueck fuer Teilstueck hinaus und holt jedes SOFORT ab.

    NACHEINANDER, und das ist eine Zusicherung und keine Absicht: Ein fertiges
    Ergebnis liegt auf der Gegenseite nur rund dreissig Minuten bereit. Wuerde
    erst alles abgesendet und spaeter eingesammelt, waere je Dokument eine
    wachsende Zahl bezahlter Ergebnisse gleichzeitig geparkt — genau das machten
    die alten Ad-hoc-Skripte (120 gleichzeitig offene Jobs, eingesammelt in einer
    zweiten Phase). So ist je Dokument hoechstens EINES offen.

    `teile` wird an Ort und Stelle gefuellt und nicht zurueckgegeben. Der Grund
    ist der Fehlerfall: Fliegt mitten in der Schleife eine Ausnahme, muss der
    Aufrufer die BIS DAHIN abgeholten Teilstuecke noch haben — sie sind bezahlt
    und gehoeren in `raw`. Ein Rueckgabewert waere in diesem Moment verloren.
    """
    for chunk, daten in stuecke:
        kodiert = base64.b64encode(daten).decode("ascii")
        job = await runpod.submit(
            file_b64=kodiert,
            backend=settings.backend,
            transport=settings.transport,
        )
        antwort = await runpod.await_result(job)
        _endzustand_uebersetzen(antwort, job)
        teile.append(assembly.unpack_response(antwort, chunk))


def _endzustand_uebersetzen(antwort: dict[str, Any], job_id: str) -> None:
    """Bewertet den Endzustand eines Jobs — Schritt 3b.

    `await_result` gibt auch `FAILED`, `CANCELLED` und `TIMED_OUT` zurueck, ohne
    zu werfen; das ist Absicht, damit die bezahlte Fehlerantwort nicht
    verlorengeht. HIER wird sie bewertet, und erst danach sieht
    `assembly.unpack_response` die Antwort — es sieht also ausschliesslich
    erfolgreiche.

    `TIMED_OUT` ist wiederholbar (es kann an der Warteschlange gelegen haben),
    `FAILED` und `CANCELLED` sind endgueltig.
    """
    zustand = antwort.get("status")
    if zustand == "COMPLETED":
        return
    if zustand == "TIMED_OUT":
        raise TransientRunPodError(
            f"Job {job_id} endete auf TIMED_OUT. Wiederholbar: Es kann an der "
            f"Warteschlange gelegen haben. Volle Antwort: {antwort}"
        )
    raise TerminalRunPodError(
        f"Job {job_id} endete auf {zustand!r}. Endgueltig. Volle Antwort: "
        f"{antwort}"
    )


async def wait_for_balance(runpod: RunPodAccess, settings: Settings) -> None:
    """Haelt an, bis das Konto wieder ueber der Pausenschwelle steht.

    Wird an zwei Stellen gebraucht: einmal vorsorglich vor dem ersten Chunk eines
    Dokuments, und einmal als Antwort auf HTTP 402 mitten im Lauf.

    Warum warten und nicht scheitern: Ein leeres Konto ist kein Fehler des
    Dokuments. Wuerde man werfen, verbrauchte die Wartezeit die drei echten
    Versuche, und ein Bestand liefe bei kurzzeitig leerem Konto reihenweise in
    `failed`. Warten kostet dagegen fast nichts — im asynchronen Betrieb ist ein
    blockierter Platz billig.

    Warum eine Warteschleife und keine Wiedervorlage mit spaeterem Termin: Eine
    Wiedervorlage erhoeht den Versuchszaehler (Feature-Verifikation Punkt 8), und
    ihn sauber vom Budget echter Fehler zu trennen hiesse, einen Sonderpfad in
    die Wiederholungs-Strategie zu bauen. Der tragende Grund ist aber die
    Einfachheit: Die Warteschleife braucht keinen zusaetzlichen Zustand, kein
    Karussell aus Jobs, keine Steuer-Zeile — und „keine Steuer-Zeile" ist eine
    ausdrueckliche Festlegung (Plan 2.4).

    Prueft alle `settings.balance_recheck_seconds` mit `asyncio.sleep` — und ist
    damit beim Herunterfahren von Haus aus abbrechbar.

    Args:
        runpod: Zum Abfragen des Kontostands.
        settings: Traegt Schwelle und Abstand.

    Returns:
        Nichts, sobald der Kontostand wieder reicht.

    Zusicherung:
        Kehrt NUR zurueck, wenn der Kontostand tatsaechlich gelesen wurde und
        ueber der Schwelle liegt. Eine FEHLGESCHLAGENE Abfrage gilt nicht als
        „Konto in Ordnung" und beendet das Warten nicht — sie wird vermerkt und
        beim naechsten Durchgang wiederholt. Der umgekehrte Weg waere schlimmer:
        wegen eines Netzwackelns weiterzumachen und in eine Wand aus
        402-Antworten zu laufen, jede davon ein bezahlter Fehlversuch.

    Raises:
        asyncio.CancelledError: Beim Herunterfahren. Nicht gefangen.

    Kosten: keine. Den Kontostand zu lesen ist frei.
    """
    while True:
        try:
            stand = await runpod.balance()
        except InsufficientBalanceError:
            # Der Kontostand liess sich nicht lesen, WEIL das Konto leer ist —
            # dann ist die Antwort dieselbe wie unten: warten.
            stand = 0.0
        except Exception:  # noqa: BLE001
            # Eine FEHLGESCHLAGENE Abfrage gilt NICHT als „Konto in Ordnung".
            # Der umgekehrte Weg waere schlimmer: wegen eines Netzwackelns
            # weiterzumachen und in eine Wand aus 402-Antworten zu laufen, jede
            # davon ein bezahlter Fehlversuch. Also: erneut versuchen.
            await asyncio.sleep(settings.balance_recheck_seconds)
            continue

        if stand >= settings.min_balance_pause_usd:
            return
        await asyncio.sleep(settings.balance_recheck_seconds)
