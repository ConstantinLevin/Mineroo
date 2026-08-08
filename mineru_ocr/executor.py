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
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

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

T = TypeVar("T")
"""Der Rueckgabetyp des Schritts, den `_mit_kontopause` wiederholt. Er ist
generisch, weil dieselbe Pausenlogik ueber dem Absenden (liefert die Job-Kennung)
und ueber dem Abholen (liefert die Antwort) steht."""

log = logging.getLogger("mineru_ocr.executor")
"""Der Logger dieses Moduls.

Er steht hier, weil die Warteschleife bei leerem Konto sonst die einzige Stelle
im Paket waere, die MINUTENLANG bis STUNDENLANG nichts tut und dabei nichts
sagt. Ein Docstring behauptete frueher, ein Fehlschlag der Kontostandsabfrage
werde „vermerkt" — das Modul kannte `logging` gar nicht, und nichts wurde
irgendwo vermerkt. Genau diese Kombination — warten, schweigen, keinen Fehler
werfen — ist im Betrieb von „laeuft" nicht zu unterscheiden."""


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
        #
        # Ueber `_ergebnis_fuer_endzustand` und NICHT unmittelbar ueber
        # `assembly.assemble`: Dieser Aufruf ist genau der, der oben geworfen
        # haben kann, und dann wuerde er hier ein zweites Mal werfen, den Handler
        # verlassen und das Schreiben ganz ausfallen lassen. Die Begruendung samt
        # dem, was das gekostet haette, steht an der Funktion.
        ergebnis = _ergebnis_fuer_endzustand(teile, vermerke)
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


def _ergebnis_fuer_endzustand(
    teile: list[assembly.ChunkArtifacts], vermerke: list[dict[str, Any]]
) -> dict[str, Any]:
    """Baut das `raw` des FEHLERPFADS — und kann dabei selbst nicht scheitern.

    Warum es diese Funktion gibt: Der Fehlerpfad rief frueher unmittelbar
    `assembly.assemble(teile)`. Das ist genau der Aufruf, der im Erfolgspfad
    geworfen haben kann — ein `content_list`-Eintrag mit nicht ganzzahligem
    `page_idx`, ein Eintrag, der kein Dict ist, ein `markdown`, das als JSON-null
    ankam. War der urspruengliche Fehler dieser Wurf, warf derselbe Aufruf im
    Fehlerpfad sofort wieder, die Ausnahme verliess den Handler, und
    `write_terminal_async` lief NIE.

    Was das kostete: Saemtliche bezahlten Teilstuecke des Laufs waren weg — sie
    lagen nur im Arbeitsspeicher. Der Job endete `failed`, die Zeile blieb `new`.
    Damit zaehlte das Dokument fuer immer als `pending` und nie als `failed`, und
    weil `OCRMegaWrapper.pending` genau die Menge ist, die ein Ordner-Durchlauf
    anfasst, bezahlte JEDER erneute Durchlauf das ganze Dokument neu und
    scheiterte deterministisch wieder an derselben Stelle. Eine Endlosschleife
    aus vollen Neubezahlungen, ohne dass irgendwo ein Fehler sichtbar wurde.

    Die Zusicherung „Endgueltige Fehler schreiben ZUERST und werfen DANN" haengt
    also daran, dass dieser Schritt nicht werfen kann. Deshalb steht er hier und
    nicht als Aufruf im Handler.

    Args:
        teile: Die bis zum Fehler abgeholten Teilstuecke. Alle bezahlt.
        vermerke: Die Vermerkliste des Laufs. Scheitert das Zusammensetzen, wird
            hier ein Eintrag ANGEHAENGT — der Leser soll sehen, dass er die
            Notfassung vor sich hat und nicht das uebliche Ergebnis.

    Returns:
        Im Normalfall das Ergebnis von `assembly.assemble`, also dieselbe Form
        wie im Erfolgspfad. Scheitert das Zusammensetzen, die Notfassung aus
        `_rohfassung_der_teile` — erkennbar an `assembly_failed`.

    Zusicherung:
        Wirft nicht. Weder bei kaputtem Inhalt noch bei kaputten Typen. Das ist
        die ganze Aufgabe dieser Funktion.

    Kosten: keine.
    """
    try:
        return assembly.assemble(teile)
    except Exception as zweiter:  # noqa: BLE001 — siehe Zusicherung: wirft nicht
        log.warning(
            "Zusammensetzen im Fehlerpfad gescheitert (%s: %s). Es wird die "
            "Rohfassung der %d bezahlten Teilstuecke geschrieben.",
            type(zweiter).__name__,
            zweiter,
            len(teile),
        )
        vermerke.append(
            {
                "kind": "zusammensetzen_gescheitert",
                "at": _jetzt(),
                "detail": f"{type(zweiter).__name__}: {zweiter}",
                "folge": (
                    "raw traegt die Rohfassung der Teilstuecke statt des "
                    "zusammengesetzten Dokuments. Die Seitenzahlen sind darin "
                    "teilstueck-lokal und NICHT auf das Original umgerechnet."
                ),
            }
        )
        return _rohfassung_der_teile(teile, zweiter)


def _rohfassung_der_teile(
    teile: list[assembly.ChunkArtifacts], grund: BaseException
) -> dict[str, Any]:
    """Alle bezahlten Teilstuecke als JSON, ohne jedes Zusammensetzen.

    Die Abruf-Regel lautet: alles holen, alles persistieren, nie filtern. Wenn
    das Zusammensetzen scheitert, ist die Antwort darauf nicht, das Bezahlte
    fallenzulassen, sondern es unbearbeitet aufzuheben — dreissig Minuten spaeter
    ist es auf der Gegenseite nicht mehr abrufbar.

    Diese Fassung setzt ausdruecklich NICHTS zusammen und rechnet insbesondere
    KEINEN Seitenversatz: Genau diese Rechnung kann die gescheiterte gewesen
    sein. `page_idx` bleibt deshalb teilstueck-lokal, und der Vermerk aus
    `_ergebnis_fuer_endzustand` sagt es dem Leser.

    Die Form ist bewusst eine ANDERE als die des Erfolgspfads — `chunks` statt
    `markdown`/`content_list` auf oberster Ebene. Wer diese Zeile spaeter liest,
    soll nicht auf den ersten Blick ein gewoehnliches Ergebnis sehen, das nur
    seltsam aussieht.

    Zusicherung:
        Wirft nicht, und das Ergebnis ist JSON-faehig. Was sich nicht nach JSON
        wandeln laesst, steht als seine Textdarstellung da — verlustbehaftet,
        aber vorhanden, und als solches gekennzeichnet.
    """
    chunks: list[dict[str, Any]] = []
    for nummer, teil in enumerate(teile):
        chunks.append(
            {
                "chunk": {
                    "first_page": _json_faehig(getattr(teil.chunk, "first_page", None)),
                    "last_page": _json_faehig(getattr(teil.chunk, "last_page", None)),
                },
                "markdown": _json_faehig(getattr(teil, "markdown", None)),
                "content_list": _json_faehig(getattr(teil, "content_list", None)),
                "images": _json_faehig(getattr(teil, "images", None)),
                "middle": _json_faehig(getattr(teil, "middle", None)),
                "layout_pdf": _json_faehig(getattr(teil, "layout_pdf", None)),
                "span_pdf": _json_faehig(getattr(teil, "span_pdf", None)),
                "envelope": _json_faehig(getattr(teil, "envelope", None)),
                "extras": _json_faehig(getattr(teil, "extras", None)),
                "nummer": nummer,
            }
        )
    return {
        "assembly_failed": {
            "detail": f"{type(grund).__name__}: {grund}",
            "hinweis": (
                "Das Zusammensetzen ist gescheitert. Hier stehen die bezahlten "
                "Teilstuecke unbearbeitet. page_idx ist teilstueck-lokal und "
                "beginnt in jedem Teilstueck wieder bei 0."
            ),
        },
        "chunks": chunks,
    }


def _json_faehig(wert: Any) -> Any:
    """Macht einen Wert JSON-faehig, ohne je zu werfen.

    Drei Faelle, in dieser Reihenfolge: Bytes werden nach Base64 gewandelt (so
    haelt es auch `assemble`, und es ist verlustfrei umkehrbar). Ein Wert, der
    sich serialisieren laesst, geht unveraendert durch. Alles andere wird zu
    seiner Textdarstellung — verlustbehaftet, aber vorhanden. Ein Dict wird
    Eintrag fuer Eintrag behandelt, damit ein einziger unbrauchbarer Wert nicht
    die ganze Abbildung in eine Textdarstellung verwandelt; dasselbe fuer Listen.

    Der Rueckfall auf die Textdarstellung ist bewusst kein Weglassen: Ein Feld,
    das hier verschwaende, waere bezahlt und weg, und niemand wuesste, dass es je
    da war.
    """
    if isinstance(wert, bytes):
        return base64.b64encode(wert).decode("ascii")
    try:
        json.dumps(wert)
    except (TypeError, ValueError):
        pass
    else:
        return wert
    if isinstance(wert, dict):
        return {str(schluessel): _json_faehig(inhalt) for schluessel, inhalt in wert.items()}
    if isinstance(wert, (list, tuple)):
        return [_json_faehig(inhalt) for inhalt in wert]
    return {"nicht_json_faehig": repr(wert), "typ": type(wert).__name__}


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

    HTTP 402 MITTEN IM LAUF wird hier abgefangen und in eine Warteschleife
    uebersetzt, statt geworfen zu werden. Das ist Regel 8 des Entwurfs („leeres
    Konto pausiert statt scheitert"), und sie war lange nicht gebaut: Der
    Docstring von `wait_for_balance` behauptete zwei Aufrufstellen, es gab genau
    eine — die vorsorgliche vor dem ersten Teilstueck. Ein 402 beim Absenden von
    Teilstueck 2 lief ungefangen nach oben, `should_run_again` stufte ihn als
    wiederholbar ein, und die Warteschlange legte den Job neu vor. Das kostete
    zweierlei: Die bereits bezahlten Teilstuecke in `teile` waren verworfen und
    wurden beim Wiederlauf neu bezahlt, und die Wiedervorlage erhoehte `attempts`
    — womit ein leerlaufendes Konto das Budget aus `max_attempts` aufzehrte,
    genau das, was die Drei-Antworten-Einteilung in `errors.py` ausschliessen
    soll.

    Absenden und Abholen pausieren GETRENNT, und das ist kein Zufall: Ein 402
    beim Abholen darf NICHT zu einem neuen Absenden fuehren. Der Job laeuft dann
    schon und ist bezahlt; ihn ein zweites Mal abzuschicken zahlte ihn ein
    zweites Mal und liesse das erste Ergebnis in seinem Dreissig-Minuten-Fenster
    verfallen.
    """
    for chunk, daten in stuecke:
        kodiert = base64.b64encode(daten).decode("ascii")

        async def absenden(kodiert: str = kodiert) -> str:
            return await runpod.submit(
                file_b64=kodiert,
                backend=settings.backend,
                transport=settings.transport,
            )

        job = await _mit_kontopause(
            absenden, runpod=runpod, settings=settings, schritt="Absenden"
        )

        async def abholen(job: str = job) -> dict[str, Any]:
            # DERSELBE Job. Ein erneuter Aufruf setzt das Abfragen fort und
            # kostet nichts; ein neues Absenden kostete das Teilstueck erneut.
            return await runpod.await_result(job)

        antwort = await _mit_kontopause(
            abholen, runpod=runpod, settings=settings, schritt="Abholen"
        )
        _endzustand_uebersetzen(antwort, job)
        teile.append(assembly.unpack_response(antwort, chunk))


_KONTOPAUSEN_JE_SCHRITT = 3
"""Wie oft ein einzelner Schritt wegen leeren Kontos pausieren darf.

Die Zahl begrenzt NICHT das Warten — `wait_for_balance` wartet, so lange das
Konto leer ist, und genau so ist es gewollt. Sie begrenzt den WIDERSPRUCH: Kehrt
`wait_for_balance` zurueck, sagt die Kontostandsabfrage, es sei wieder Geld da.
Antwortet der Endpoint daraufhin ERNEUT mit 402, widersprechen sich zwei Quellen.
Dann weiter zu warten hiesse, endlos auf eine Bedingung zu warten, die nach der
einen Quelle laengst erfuellt ist — der stille Stillstand, gegen den auch die
Behandlung der Konfigurationsfehler in `wait_for_balance` steht. Nach drei
solchen Runden wird der Widerspruch benannt und geworfen, statt ihn auszusitzen.
"""


async def _mit_kontopause(
    aufruf: Callable[[], Awaitable[T]],
    *,
    runpod: RunPodAccess,
    settings: Settings,
    schritt: str,
) -> T:
    """Fuehrt `aufruf` aus und pausiert bei HTTP 402, statt zu werfen.

    Args:
        aufruf: Der zu wiederholende Schritt, ohne Argumente. Er MUSS
            wiederholbar sein, ohne zusaetzlich Geld zu kosten — deshalb steht
            hier das Absenden EINES Teilstuecks oder das Fortsetzen des Abfragens
            EINES Jobs, nie beides zusammen.
        runpod: Fuer die Kontostandsabfrage waehrend der Pause.
        settings: Fuer Schwelle und Abfragetakt der Pause.
        schritt: Nur fuer die Meldung — „Absenden" oder „Abholen".

    Returns:
        Was `aufruf` zurueckgibt, sobald er ohne 402 durchlief.

    Zusicherung:
        Der Procrastinate-Versuchszaehler bleibt unberuehrt: Es wird gewartet,
        nicht geworfen. Jede andere Ausnahme geht unveraendert nach oben.

    Raises:
        TerminalRunPodError: Der Endpoint antwortet auch dann noch mit 402, wenn
            die Kontostandsabfrage ausreichendes Guthaben meldet — und zwar
            `_KONTOPAUSEN_JE_SCHRITT` Mal hintereinander. Das ist ein Widerspruch
            zwischen zwei Quellen und keine Geldfrage mehr.

    Kosten: die des uebergebenen Schritts, einmal je Runde.
    """
    pausen = 0
    while True:
        try:
            return await aufruf()
        except InsufficientBalanceError as leer:
            pausen += 1
            if pausen > _KONTOPAUSEN_JE_SCHRITT:
                raise TerminalRunPodError(
                    f"{schritt}: Der Endpoint antwortet mit HTTP 402 (leeres "
                    f"Konto), obwohl die Kontostandsabfrage nach jeder der "
                    f"{_KONTOPAUSEN_JE_SCHRITT} Pausen ausreichendes Guthaben "
                    f"gemeldet hat. Die beiden Quellen widersprechen einander; "
                    f"weiteres Warten wuerde auf eine Bedingung warten, die "
                    f"laut Kontostand schon erfuellt ist. Urspruengliche "
                    f"Meldung: {leer}"
                ) from leer
            log.warning(
                "%s wegen leeren Kontos pausiert (Runde %d von %d): %s",
                schritt,
                pausen,
                _KONTOPAUSEN_JE_SCHRITT,
                leer,
            )
            await wait_for_balance(runpod, settings)


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
    Dokuments (`process_document`), und einmal als Antwort auf HTTP 402 mitten im
    Lauf — die zweite Stelle ist `_mit_kontopause`, das Absenden und Abholen
    jedes Teilstuecks umschliesst. Dieser Satz stand hier lange, ohne dass es die
    zweite Stelle gab; wer ihn liest, soll beide nachschlagen koennen.

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
        ueber der Schwelle liegt. Eine VORUEBERGEHEND fehlgeschlagene Abfrage
        gilt nicht als „Konto in Ordnung" und beendet das Warten nicht — sie wird
        protokolliert und beim naechsten Durchgang wiederholt. Der umgekehrte Weg
        waere schlimmer: wegen eines Netzwackelns weiterzumachen und in eine Wand
        aus 402-Antworten zu laufen, jede davon ein bezahlter Fehlversuch.

        Ein ENDGUELTIGER Fehler der Abfrage beendet das Warten dagegen sofort,
        indem er nach oben geht. Frueher fing diese Schleife jede Ausnahme und
        wartete weiter, und das hatte eine Folge, die im Betrieb nicht von
        „laeuft" zu unterscheiden war: Ein widerrufener Schluessel oder ein
        geloeschter Endpoint (HTTP 401/403 — `ConfigurationError`, laut
        `errors.py` „immer endgueltig, nie ein Grund fuer einen Retry") liess
        JEDES Dokument vor seinem ersten Teilstueck in dieser Schleife haengen,
        endlos, ohne Log, ohne Fehler, ohne Terminal-Schreiben. Der ganze Lauf
        stand still und sah dabei aus wie Arbeit.

        Endgueltig oder nicht entscheidet `errors.should_run_again` und nicht
        eine eigene Fallunterscheidung an dieser Stelle. Das ist dieselbe Regel
        wie im Fehlerpfad von `process_document`: Wer hier eine zweite
        Einteilung einfuehrt, stellt genau den Fehler wieder her, wegen dessen
        `should_run_again` ueberhaupt entstanden ist.

    Raises:
        asyncio.CancelledError: Beim Herunterfahren. Nicht gefangen.
        ConfigurationError: Der Schluessel oder der Endpoint taugt nicht mehr.
            Weiterwarten hiesse hier, auf etwas zu warten, das von selbst nie
            eintritt.
        TerminalRunPodError: Ebenso, fuer die uebrigen endgueltigen Faelle der
            Kontostandsabfrage.

    Kosten: keine. Den Kontostand zu lesen ist frei.
    """
    while True:
        try:
            stand = await runpod.balance()
        except InsufficientBalanceError:
            # Der Kontostand liess sich nicht lesen, WEIL das Konto leer ist —
            # dann ist die Antwort dieselbe wie unten: warten.
            stand = 0.0
        except Exception as fehler:  # noqa: BLE001 — Einteilung folgt sofort
            if not should_run_again(fehler):
                # Endgueltig. NICHT aussitzen — siehe Zusicherung.
                log.error(
                    "Kontostandsabfrage endgueltig gescheitert (%s: %s). Das "
                    "Warten wird abgebrochen; ein widerrufener Schluessel oder "
                    "ein geloeschter Endpoint heilt nicht von selbst.",
                    type(fehler).__name__,
                    fehler,
                )
                raise
            # Voruebergehend: erneut versuchen — aber sichtbar. Ohne diese Zeile
            # ist eine Schleife, die minuten- bis stundenlang nichts tut, von
            # einem laufenden Dokument nicht zu unterscheiden.
            log.warning(
                "Kontostandsabfrage fehlgeschlagen (%s: %s). Erneuter Versuch "
                "in %.0f s.",
                type(fehler).__name__,
                fehler,
                settings.balance_recheck_seconds,
            )
            await asyncio.sleep(settings.balance_recheck_seconds)
            continue

        if stand >= settings.min_balance_pause_usd:
            return
        log.info(
            "Konto bei %.2f USD, Pausenschwelle %.2f USD. Warten, erneute "
            "Pruefung in %.0f s.",
            stand,
            settings.min_balance_pause_usd,
            settings.balance_recheck_seconds,
        )
        await asyncio.sleep(settings.balance_recheck_seconds)
