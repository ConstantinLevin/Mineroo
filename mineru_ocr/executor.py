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

from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .runpod import RunPodClient


async def process_document(
    sha256: str,
    *,
    settings: Settings,
    runpod: RunPodClient,
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
    raise NotImplementedError


async def wait_for_balance(runpod: RunPodClient, settings: Settings) -> None:
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
    raise NotImplementedError
