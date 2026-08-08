"""Der einzige Ort, der mit RunPod spricht. Alle harten Plattform-Regeln wohnen hier.

Jede Regel in diesem Modul ist aus einem bezahlten Fehler entstanden, und jede ist
so gebaut, dass sie nicht durch Disziplin eingehalten werden muss, sondern durch
Bauart. Die Reihenfolge ist die des Betriebshandbuchs:

`/runsync` gibt es hier nicht — nicht als Voreinstellung, nicht als Parameter,
gar nicht. Der Grund: Nach einer Ruhephase braucht der Endpoint rund achteinhalb
Minuten fuer den Kaltstart, `/runsync` wartet aber nur sechzig Sekunden. Man
bekommt dann keine Fehlermeldung, sondern eine Zeitueberschreitung auf einen Job,
der in Wahrheit laeuft und gleich bezahlt sein wird. Eine Methode, die es nicht
gibt, kann niemand versehentlich benutzen.

Ein `formats`-Feld wird NIE mitgeschickt. Bei `tarball_b64` waere es ohnehin
wirkungslos, aber die Regel ist absoluter als ihr Anlass: Wer die Ausgabe beim
Absenden filtert, entscheidet vor dem Ansehen, was er spaeter braucht — und die
Antwort ist dreissig Minuten spaeter weg. Beide alten Ad-hoc-Skripte hatten
`formats: ["markdown"]` gesetzt, und genau das ist die Regel, die daraus wurde:
alles holen, alles behalten, nie filtern.

Optionale Felder werden WEGGELASSEN, nie als JSON-`null` gesendet. Der Worker
unterscheidet zwischen „nicht gesetzt" und „ausdruecklich null", und `null` fuehrt
zu Verhalten, das wie ein Fehler des Modells aussieht, aber keiner ist.

Der API-Schluessel reist ausschliesslich im Authorization-Header. Nie in einer
URL, nie in einer Log-Zeile, nie in einer Fehlermeldung — auch dann nicht, wenn
die Fehlermeldung die volle Anfrage zeigen wuerde.

Was ueber GraphQL laeuft, braucht einen Browser-tauglichen User-Agent, sonst
antwortet der Dienst nicht. Das ist keine Vermutung, sondern gemessen.

Und schliesslich: NICHTS in diesem Modul veraendert eine bezahlte Ressource. Es
gibt keine Methode, die `workersMax` setzt, ein Template aendert oder einen
Endpoint anlegt. Der Preflight stellt fest und meldet; Aendern ist eine
Entscheidung des Auftraggebers und keine des Programms (Plan 8, Regel 10).
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Any, Final

import httpx

from .config import Settings
from .errors import (
    ConfigurationError,
    InsufficientBalanceError,
    ResultLostError,
    TerminalRunPodError,
    TransientRunPodError,
)

API_BASE: Final = "https://api.runpod.ai/v2"
"""Wurzel der Serverless-Schnittstelle. Der Endpoint haengt hinten dran.

Beachte den Wirtsnamen: `api.runpod.ai` (Job-Schnittstelle) ist NICHT
`api.runpod.io` (Verwaltung) und nicht `rest.runpod.io` (siehe `REST_BASE`).
Drei Wirtsnamen, die sich um einen Buchstaben unterscheiden und verschiedene
Dinge tun — die Verwechslung erzeugt ein HTTP 401, das nach einem falschen
Schluessel aussieht und keiner ist."""

REST_BASE: Final = "https://rest.runpod.io/v1"
"""Wurzel der Verwaltungs-Schnittstelle: Endpoint-Konfiguration und Worker-Liste.

Diese Wurzel ist in diesem Projekt BEWIESEN: Der konservierte Schnappschuss vom
05.08.2026 (`artifacts/2026-08-05-doku-verifikationstest/endpoint-snapshot.json`)
traegt als Quelle woertlich
`GET https://rest.runpod.io/v1/endpoints/l79lebhinxaqjz?includeWorkers=true`, und
das Betriebshandbuch nennt dieselbe Adresse als Nachpruef-Ort fuer `workersMax`.

OFFENER PUNKT, ausdruecklich benannt statt still uebergangen: RunPods eigene
Werkzeugbeschreibung fuehrt diese Fassung als VERALTET und verweist auf eine
Nachfolgerin unter `https://api.runpod.io/v2`. Deren Antwortform ist hier NICHT
geprueft — es gibt keinen Schnappschuss davon, und sie ungeprueft zu benutzen
hiesse, die Feldnamen zu erfinden. Deshalb steht hier die bewiesene Fassung, und
der Wechsel ist eine Zeile, sobald ein Lauf gegen die neue Fassung ihre Form
gezeigt hat. Die Alternative — auf gut Glueck gegen die neue Fassung zu bauen —
faellt erst im Betrieb auf, und dann an einer Stelle, die Geld ausgibt."""

GRAPHQL_URL: Final = "https://api.runpod.io/graphql"
"""Fuer Kontostand, Worker-Liste und Template-Angaben — Dinge, die die
Serverless-Schnittstelle nicht kennt."""

BROWSER_USER_AGENT: Final = "Mozilla/5.0"
"""Ohne einen browser-aehnlichen User-Agent antwortet der GraphQL-Dienst nicht.
Gemessen, nicht vermutet."""

TERMINAL_STATUSES: Final = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})
"""Job-Zustaende, nach denen kein weiteres Abfragen mehr etwas aendert."""

RUNNING_STATUSES: Final = frozenset({"IN_QUEUE", "IN_PROGRESS", "RUNNING"})
"""Job-Zustaende, bei denen weiter abgefragt wird. `RUNNING` steht hier, weil es
im Betrieb beobachtet wurde, auch wenn die Dokumentation nur `IN_PROGRESS`
nennt — ein unbekannter Zustand wuerde sonst als Fehler behandelt und ein
laufender Job abgebrochen."""


class RunPodClient:
    """Zugang zum RunPod-Endpoint. Ein Objekt je Daemon-Lauf, geteilt von allen Tasks.

    Besitz und Lebensdauer: Wer den Client baut, schliesst ihn (`aclose`, oder als
    Kontext-Manager). Wird ein `httpx.AsyncClient` hereingereicht, gehoert er dem
    Aufrufer und wird NICHT mitgeschlossen — das ist die Naht, an der die Tests
    einen `MockTransport` einhaengen, ohne dass dieses Modul etwas von Tests weiss.

    Nebenlaeufigkeit: Ja, ausdruecklich. Vierundzwanzig Dokumente teilen sich
    einen Client; `httpx.AsyncClient` ist dafuer gebaut und haelt seinen
    Verbindungspool selbst. Es gibt keinen veraenderlichen Zustand an diesem
    Objekt, den zwei Koroutinen sich streitig machen koennten.

    Kosten: `submit` kostet Geld, sobald ein Worker den Job aufnimmt. Alles
    andere in dieser Klasse liest nur.
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """Baut den Zugang.

        Args:
            settings: Traegt Endpoint, Schluessel und alle Zeitschranken.
            http: Optionaler, fertiger HTTP-Client. None heisst: einen eigenen
                anlegen. Ein hereingereichter Client wird NICHT veraendert —
                insbesondere wird ihm kein Header aufgezwungen; die Kopfzeilen
                setzt dieses Modul je Anfrage.

        Wie der eigene Client gebaut wird, und warum es genau so sein muss:

            `timeout=httpx.Timeout(connect=…, read=…, write=…, pool=…)` aus den
            vier Feldern in `settings`. NICHT die Voreinstellung: Die ist EINE
            Schranke von fuenf Sekunden fuer alle vier Phasen zusammen, und
            dagegen ist ein Hochladen von 9,3 MB nicht zu schaffen. Begruendung in
            voller Laenge an `Settings.http_write_timeout`.

            `limits=httpx.Limits(max_keepalive_connections=settings.http_max_keepalive)`,
            damit nicht ein Teil der gleichzeitig betreuten Dokumente bei jeder
            Abfrage die Verbindung neu aufbaut.

            AUSDRUECKLICH KEIN `httpx.AsyncHTTPTransport(retries=N)`. Das sieht
            nach der Loesung fuer Netzwackeln aus, ist es aber nicht: Es
            wiederholt NUR Verbindungsfehler, keine 5xx und keine
            Lese-Zeitueberschreitungen — und auf dem Absende-Pfad waere es
            schaedlich. Ein wiederholtes `POST /run` nach einem Verbindungsfehler
            kann einen ZWEITEN bezahlten Job erzeugen, dessen Kennung der
            Aufrufer nie zu sehen bekommt. Wiederholt wird in diesem Paket an
            genau einer Stelle, und die heisst `OCRRetryStrategy`.
        """
        self._settings = settings
        self._eigener_client = http is None
        self._http = http if http is not None else httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=settings.http_read_timeout,
                write=settings.http_write_timeout,
                pool=settings.http_pool_timeout,
            ),
            limits=httpx.Limits(
                max_keepalive_connections=settings.http_max_keepalive
            ),
        )

    async def __aenter__(self) -> RunPodClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Schliesst den selbst angelegten HTTP-Client. Ein hereingereichter bleibt offen."""
        if self._eigener_client:
            await self._http.aclose()

    # ----------------------------------------------------------------------
    # Das Innere: Kopfzeilen, Aufrufe, Fehlerzuordnung
    # ----------------------------------------------------------------------

    def _kopfzeilen(self) -> dict[str, str]:
        """Der Schluessel reist ausschliesslich hier — nie in einer URL.

        Warum das eine eigene Methode ist und nicht ein Feld am Client: Ein Feld
        am `httpx.AsyncClient` wuerde einem HEREINGEREICHTEN Client aufgezwungen,
        und der gehoert dem Aufrufer. So bleibt die Naht sauber, an der die Tests
        einen Transport einhaengen.
        """
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    def _pruefen(self, antwort: httpx.Response, was: str) -> None:
        """Ordnet einen Antwortcode der richtigen Ausnahme zu.

        DIESE ZUORDNUNG ENTSCHEIDET UEBER GELD, und deshalb steht sie an einer
        Stelle statt an jeder Aufrufstelle neu: Was hier als wiederholbar gilt,
        wird noch zweimal bezahlt; was als endgueltig gilt, wird aufgegeben.

        Der Text der Antwort geht in die Meldung — aber NIE die Kopfzeilen und
        nie die Anfrage: Dort steht der Schluessel.
        """
        code = antwort.status_code
        if code < 400:
            return

        # Der Text kann alles Moegliche sein; wir zeigen ihn, weil die Meldungen
        # der Gegenseite in der Fehlersuche das Wertvollste sind.
        text = antwort.text

        if code == 402:
            raise InsufficientBalanceError(
                f"{was}: HTTP 402, das Konto reicht nicht. {text}"
            )
        if code in (401, 403):
            raise ConfigurationError(
                f"{was}: HTTP {code}, der Zugang taugt nicht. {text}\n"
                f"Zu pruefen ist ZWEIERLEI, nicht nur der Schluessel: Auch eine "
                f"URL auf einen geloeschten Endpoint antwortet mit 401 (im "
                f"Betrieb schon vorgekommen). Also erst nachsehen, ob "
                f"{self._settings.endpoint_id} ueberhaupt noch existiert."
            )
        if code == 404:
            raise ResultLostError(
                f"{was}: HTTP 404. Entweder ist die Aufbewahrung von rund "
                f"dreissig Minuten abgelaufen, oder die Kennung ist unbekannt. "
                f"{text}"
            )
        if code >= 500:
            raise TransientRunPodError(
                f"{was}: HTTP {code} auf der Gegenseite. {text}"
            )
        raise TerminalRunPodError(f"{was}: HTTP {code}. {text}")

    async def _anfragen(
        self,
        methode: str,
        url: str,
        was: str,
        *,
        json_daten: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fuehrt eine Anfrage aus und gibt die geparste Antwort zurueck.

        Transportfehler werden zu `TransientRunPodError` — ein Netzwackeln ist
        wiederholbar. Eine unlesbare Antwort dagegen ist endgueltig: Ein zweiter
        Versuch kauft dieselbe unlesbare Antwort.
        """
        try:
            antwort = await self._http.request(
                methode, url, headers=self._kopfzeilen(), json=json_daten
            )
        except httpx.HTTPError as fehler:
            raise TransientRunPodError(f"{was}: Transportfehler: {fehler}") from fehler

        self._pruefen(antwort, was)

        try:
            geparst = antwort.json()
        except ValueError as fehler:
            raise TerminalRunPodError(
                f"{was}: Antwort ist kein lesbares JSON: {fehler}. "
                f"Antworttext: {antwort.text}"
            ) from fehler
        if not isinstance(geparst, dict):
            raise TerminalRunPodError(
                f"{was}: Antwort ist {type(geparst).__name__}, erwartet war ein "
                f"Objekt. Antworttext: {antwort.text}"
            )
        return geparst

    async def _graphql(self, abfrage: str, was: str) -> dict[str, Any]:
        """Fragt ueber GraphQL — mit browser-tauglichem User-Agent.

        Ohne den antwortet der Dienst nicht. Das ist gemessen und nicht vermutet
        (Betriebshandbuch); ohne diese Kopfzeile bekommt man eine Antwort, die
        wie ein Rechteproblem aussieht und keines ist.
        """
        kopfzeilen = self._kopfzeilen()
        kopfzeilen["User-Agent"] = BROWSER_USER_AGENT
        try:
            antwort = await self._http.post(
                GRAPHQL_URL, headers=kopfzeilen, json={"query": abfrage}
            )
        except httpx.HTTPError as fehler:
            raise TransientRunPodError(f"{was}: Transportfehler: {fehler}") from fehler

        self._pruefen(antwort, was)
        geparst = antwort.json()

        # GraphQL antwortet auf Fehler mit HTTP 200 und einem `errors`-Feld. Wer
        # nur den Antwortcode prueft, haelt eine Fehlerantwort fuer ein Ergebnis
        # und liest dann `None` als „Kontostand null".
        if geparst.get("errors"):
            raise TerminalRunPodError(f"{was}: GraphQL meldet {geparst['errors']}")
        daten = geparst.get("data")
        if not isinstance(daten, dict):
            raise TerminalRunPodError(
                f"{was}: GraphQL-Antwort ohne `data`-Feld: {geparst}"
            )
        return daten

    # ----------------------------------------------------------------------
    # Jobs
    # ----------------------------------------------------------------------

    async def submit(
        self,
        *,
        file_b64: str,
        backend: str,
        transport: str,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> str:
        """Reicht einen Job ein. AB HIER LAEUFT GELD.

        Baut den kanonischen Aufruf des Job-Contracts:
        `POST {API_BASE}/{endpoint_id}/run` mit
        `{"input": {"file_b64": ..., "backend": ..., "transport": ...}}`.

        Args:
            file_b64: Die Datei oder der Ausschnitt, Base64-kodiert.
            backend: Aus den Einstellungen durchgereicht, nicht hier gewaehlt.
            transport: Ebenso. Im Betrieb immer `tarball_b64`.
            start_page: Erste Seite, null-basiert und einschliessend — laesst die
                Engine SELBST schneiden, statt dass wir vorher zerlegen. Der
                Normalweg benutzt das NICHT (wir schneiden lokal, siehe Vorspann
                von `pdf.py`), aber der Parameter steht hier, weil der
                Job-Contract ihn fuehrt und das Betriebshandbuch ihn als Abhilfe
                gegen das Antwort-Cap nennt. Ein Zugang, der weniger kann als die
                Schnittstelle, verbaut spaetere Wege ohne Not.
            end_page: Letzte Seite, null-basiert und EINSCHLIESSEND; `-1` heisst
                „bis zum Ende" und ist die Voreinstellung der Engine. Wir senden
                keine `-1`, sondern lassen das Feld weg, wenn wir es nicht
                brauchen.

        Returns:
            Die Job-Kennung.

        Zusicherung:
            Die gesendete Nutzlast enthaelt KEIN `formats`-Feld und KEINEN
            `null`-Wert. Felder, die None sind, werden weggelassen. Der
            Schluessel steht ausschliesslich im Header.

            Die Kennung wird zurueckgegeben, BEVOR irgendetwas anderes geschieht —
            der Aufrufer soll sie festhalten koennen, bevor er weiterarbeitet. Das
            ist die Lehre aus dem Schreiben-vor-dem-naechsten-Absenden der alten
            Skripte: Eine verlorene Job-Kennung ist ein bezahltes Ergebnis, das
            niemand mehr abholen kann.

        Raises:
            InsufficientBalanceError: HTTP 402.
            ConfigurationError: HTTP 401 oder 403 — der Schluessel taugt nicht.
                Kein Retry: Wiederholen macht einen ungueltigen Schluessel nicht
                gueltig.
            TransientRunPodError: Transportfehler oder HTTP 5xx.
            TerminalRunPodError: Alles uebrige, insbesondere HTTP 400 (zu grosse
                Nutzlast, unbrauchbare Eingabe).

        Kosten: JA. Ab der Annahme laeuft die Abrechnung, sobald ein Worker den
        Job aufnimmt.
        """
        # Feld fuer Feld aufgebaut, nicht aus einer Abbildung gefiltert. Der
        # Unterschied ist die Regel: Wer eine Abbildung baut und dann `None`
        # herausfiltert, hat den `null`-Wert bereits einmal gehabt — und die
        # naechste Aenderung vergisst den Filter. Hier kann kein `null` entstehen.
        eingabe: dict[str, Any] = {
            "file_b64": file_b64,
            "backend": backend,
            "transport": transport,
        }
        if start_page is not None:
            eingabe["start_page"] = start_page
        if end_page is not None:
            eingabe["end_page"] = end_page
        # Ein `formats`-Feld steht hier NICHT und kann hier nicht entstehen.

        antwort = await self._anfragen(
            "POST",
            f"{API_BASE}/{self._settings.endpoint_id}/run",
            "Job einreichen",
            json_daten={"input": eingabe},
        )

        kennung = antwort.get("id")
        if not isinstance(kennung, str) or not kennung:
            # Der schlimmste denkbare Zustand: Der Job kann angenommen und damit
            # bezahlt sein, und wir kennen seine Kennung nicht. Deshalb wird die
            # volle Antwort mitgegeben — sie ist das Einzige, womit man ihn
            # spaeter noch suchen koennte.
            raise TerminalRunPodError(
                f"Job einreichen: Antwort ohne brauchbare Kennung. Der Job kann "
                f"trotzdem angenommen und damit bezahlt sein. Volle Antwort: "
                f"{antwort}"
            )
        return kennung

    async def status(self, job_id: str) -> dict[str, Any]:
        """Fragt einen Job EINMAL ab und gibt die volle Antwort zurueck.

        Args:
            job_id: Die Kennung aus `submit`.

        Returns:
            Die vollstaendige Antwort, unveraendert und ungefiltert — genau das,
            was spaeter in `documents.raw` landet. Es wird nichts herausgegriffen
            und nichts weggelassen: Was hier ankommt, ist die einzige dauerhafte
            Kopie eines bezahlten Ergebnisses, und dreissig Minuten spaeter gibt
            es sie nirgends mehr.

        Raises:
            ResultLostError: HTTP 404. Das Ergebnis ist nicht mehr abrufbar (die
                Aufbewahrung von rund dreissig Minuten ist abgelaufen) oder die
                Kennung ist unbekannt. ENDGUELTIG — weiter abzufragen kann nie
                mehr etwas liefern, und ein neuer Versuch wuerde den Parse ein
                zweites Mal bezahlen, ohne die Ursache zu beheben.
            TransientRunPodError: Transportfehler oder HTTP 5xx — hier wird
                weiter abgefragt.
            ConfigurationError: HTTP 401 oder 403.

        Kosten: keine. Abfragen ist frei.
        """
        return await self._anfragen(
            "GET",
            f"{API_BASE}/{self._settings.endpoint_id}/status/{job_id}",
            f"Job {job_id} abfragen",
        )

    async def await_result(self, job_id: str) -> dict[str, Any]:
        """Fragt einen Job ab, bis er terminal ist, und gibt die letzte Antwort zurueck.

        Wartet zwischen den Abfragen `settings.poll_interval_seconds` mit
        `asyncio.sleep`. Das ist kein Detail, sondern die Stelle, an der ein
        Abbruch greift: Eine Warteschleife aus `asyncio.sleep` ist von Haus aus
        abbrechbar — beim Beenden des Daemons wird die Koroutine hier mit
        `CancelledError` geweckt und laeuft nicht dreissig Minuten weiter
        (Feature-Verifikation Punkt 3).

        Args:
            job_id: Die Kennung aus `submit`.

        Returns:
            Die volle Antwort im terminalen Zustand — auch bei `FAILED`. Diese
            Methode wirft NICHT, weil ein Job scheiterte; sie gibt zurueck, was
            da ist. Ob ein Endzustand ein Fehler ist, entscheidet der Aufrufer,
            und er schreibt die Antwort vorher in `raw` (Plan 5.1). Wuerde hier
            geworfen, waere die bezahlte Fehlerantwort verloren.

        Eine FEHLGESCHLAGENE Abfrage bricht das Warten NICHT ab. Sie wird mit
        wachsendem Abstand wiederholt, bis das Zeitbudget erschoepft ist. Das ist
        der wichtigste Einzelfall dieser Schleife: Ein Netzwackeln heisst nicht,
        dass das Ergebnis weg ist — es liegt auf der Gegenseite noch bis zu
        dreissig Minuten bereit. Wer hier abbricht, wirft ein bezahltes, noch
        abholbares Ergebnis weg und bezahlt es beim naechsten Anlauf ein zweites
        Mal. Nur `ResultLostError` (404) beendet das Warten sofort, denn dann ist
        wirklich nichts mehr da.

        Zusicherung:
            Der Rueckgabewert hat einen Status aus `TERMINAL_STATUSES`.

        Raises:
            TransientRunPodError: `settings.job_timeout_seconds` ueberschritten.
                Wiederholbar: Es kann an der Warteschlange gelegen haben.
            ResultLostError: siehe `status`. Ebenfalls wiederholbar — der Job
                wird dann neu geschickt, nicht das Dokument aufgegeben.
            asyncio.CancelledError: Der Daemon faehrt herunter. Wird NICHT
                gefangen und nicht in etwas anderes verwandelt — der Job endet
                dann `aborted`, das Dokument bleibt `new` und laeuft beim
                naechsten Einreihen von vorn (Plan 5.3).

        Kosten: keine ueber die von `submit` hinaus.
        """
        ende = time.monotonic() + self._settings.job_timeout_seconds
        abstand = self._settings.poll_interval_seconds
        letzter_fehler: Exception | None = None

        while True:
            if time.monotonic() >= ende:
                raise TransientRunPodError(
                    f"Job {job_id} wurde in "
                    f"{self._settings.job_timeout_seconds} Sekunden nicht "
                    f"terminal. Wiederholbar: Es kann an der Warteschlange "
                    f"gelegen haben. Letzter Fehler beim Abfragen: "
                    f"{letzter_fehler}"
                )

            try:
                antwort = await self.status(job_id)
            except ResultLostError:
                # Nur DAS beendet das Warten sofort: Dann ist wirklich nichts
                # mehr da, und weiter abzufragen kann nie mehr etwas liefern.
                raise
            except TransientRunPodError as fehler:
                # Alles andere beendet es NICHT. Das ist der wichtigste
                # Einzelfall dieser Schleife: Ein Netzwackeln heisst nicht, dass
                # das Ergebnis weg ist — es liegt auf der Gegenseite noch bis zu
                # dreissig Minuten bereit. Wer hier abbricht, wirft ein
                # bezahltes, noch abholbares Ergebnis weg und bezahlt es beim
                # naechsten Anlauf ein zweites Mal.
                letzter_fehler = fehler
                abstand = min(abstand * 2, 60.0)
                await asyncio.sleep(abstand)
                continue

            zustand = antwort.get("status")
            if zustand in TERMINAL_STATUSES:
                return antwort
            if zustand not in RUNNING_STATUSES:
                # Ein unbekannter Zustand wird NICHT als Fehler behandelt und
                # NICHT als terminal: Beides koennte einen laufenden, bezahlten
                # Job wegwerfen. Weiterfragen kostet nichts; die Zeitschranke
                # oben begrenzt es ohnehin.
                letzter_fehler = TransientRunPodError(
                    f"Job {job_id} meldet den unbekannten Zustand {zustand!r} — "
                    f"weder terminal noch laufend. Es wird weiter abgefragt."
                )

            # Nach einer erfolgreichen Abfrage zurueck auf den normalen Takt.
            abstand = self._settings.poll_interval_seconds
            await asyncio.sleep(abstand)

    # ----------------------------------------------------------------------
    # Konto und Endpoint — ausschliesslich lesend
    # ----------------------------------------------------------------------

    async def balance(self) -> float:
        """Liest den aktuellen Kontostand in US-Dollar.

        Wird an drei Stellen gebraucht: im Preflight gegen
        `min_balance_start_usd`, einmal vor dem ersten Chunk eines Dokuments, und
        in der Warteschleife bei leerem Konto. Jurgens Vorgabe aus dem Braindump
        verlangt, dass so etwas beim Zugriff live geholt und nicht zwischengelagert
        wird — „dynmiasche attribute wie runpodcredit […] die per getter direkt
        von der api gegettet werden wenn man darauf zugreift" (Plan 2.1).

        Returns:
            Der Kontostand.

        Raises:
            TransientRunPodError: Die Abfrage schlug fehl. Wichtig fuer den
                Aufrufer: Ein FEHLGESCHLAGENER Kontostand ist NICHT dasselbe wie
                ein leeres Konto. Die Arbeit wegen einer misslungenen Abfrage
                anzuhalten waere so falsch wie sie bei leerem Konto weiterlaufen
                zu lassen.

        Kosten: keine.
        """
        angaben = await self.account()
        stand = angaben.get("clientBalance")
        if not isinstance(stand, (int, float)) or isinstance(stand, bool):
            raise TransientRunPodError(
                f"Kontostand: Das Feld `clientBalance` fehlt oder ist "
                f"{type(stand).__name__} statt einer Zahl. Volle Antwort: "
                f"{angaben}\nWICHTIG fuer den Aufrufer: Das ist eine "
                f"fehlgeschlagene ABFRAGE und NICHT ein leeres Konto. Die Arbeit "
                f"deswegen anzuhalten waere so falsch, wie sie bei wirklich "
                f"leerem Konto weiterlaufen zu lassen."
            )
        return float(stand)

    async def account(self) -> dict[str, Any]:
        """Liest die Kontoangaben als Ganzes (Kontostand, Ausgaben, Grenzen).

        Die Felder sind am konservierten Schnappschuss vom 05.08.2026 abgelesen
        (`user-snapshot.json`, Quelle `runpodctl user`): `clientBalance`,
        `currentSpendPerHr`, `spendLimit`, dazu `id`, `email` und die
        Benachrichtigungs-Schalter. Abgefragt werden nur die drei, die zaehlen —
        die Kennung und die Adresse braucht niemand, und sie in einer Fehlermeldung
        stehen zu haben ist unnoetig.

        Returns:
            Die rohen Angaben, ungefiltert.

        Kosten: keine.
        """
        daten = await self._graphql(
            "query { myself { clientBalance currentSpendPerHr spendLimit } }",
            "Kontoangaben lesen",
        )
        ich = daten.get("myself")
        if not isinstance(ich, dict):
            raise TerminalRunPodError(
                f"Kontoangaben lesen: Antwort ohne `myself`-Objekt: {daten}"
            )
        return ich

    async def workers(self) -> list[dict[str, Any]]:
        """Liest die Worker EINZELN auf, nicht als Summe.

        Warum ausdruecklich einzeln: Die zusammengefassten Zaehler des Endpoints
        luegen — sie zeigen Worker, die es nicht mehr gibt, und verschweigen
        welche, die es gibt. Wer auf ihrer Grundlage entscheidet, entscheidet auf
        einer Erfindung (Plan 8, Regel 11). Die Einzelliste ist die Wahrheit.

        Returns:
            Ein Eintrag je Worker mit Kennung, Zustand und Angaben zur laufenden
            Fassung. Leere Liste heisst: gerade laeuft keiner — das ist der
            NORMALFALL bei einem Endpoint, der auf null herunterskaliert, und
            kein Fehler.

        Kosten: keine.
        """
        angaben = await self.endpoint()
        liste = angaben.get("workers")
        if liste is None:
            # Kein `workers`-Feld heisst NICHT „keine Worker". Es heisst, dass
            # die Abfrage sie nicht angefordert hat oder die Gegenseite sie nicht
            # mehr so ausliefert — und das ist ein Unterschied, den man nicht
            # stillschweigend zu einer leeren Liste machen darf, weil genau
            # daraus die Aussage „es laeuft keiner" wuerde.
            raise TerminalRunPodError(
                f"Worker-Liste: Die Endpoint-Antwort traegt kein `workers`-Feld, "
                f"obwohl mit `includeWorkers=true` gefragt wurde. Vorhandene "
                f"Felder: {sorted(angaben)}"
            )
        if not isinstance(liste, list):
            raise TerminalRunPodError(
                f"Worker-Liste: `workers` ist {type(liste).__name__} statt einer "
                f"Liste."
            )
        return liste

    async def endpoint(self) -> dict[str, Any]:
        """Liest die Konfiguration des Endpoints.

        Returns:
            Die rohen Angaben, darunter `workersMin`, `workersMax`,
            `idleTimeout`, `executionTimeoutMs`, die Rechenzentren und die
            Template-Kennung. Der Preflight vergleicht das gegen die Sollwerte
            des Betriebshandbuchs.

            Die Rechenzentren sind dabei kein Nebenwert: Der Steuer-Bestand darf
            nur in der EU verarbeitet werden. Der Preflight MELDET die
            Konfiguration mit, statt ein Flag danach zu richten — die Entscheidung
            gehoert dem Menschen (Plan 2.4, kein `pii`-Flag im Client).

        Kosten: keine. Liest, veraendert nie.
        """
        return await self._anfragen(
            "GET",
            f"{REST_BASE}/endpoints/{self._settings.endpoint_id}"
            f"?includeWorkers=true",
            "Endpoint-Konfiguration lesen",
        )

    async def image_tag(self) -> str | None:
        """Liest, welches Worker-Image das Template gerade vorgibt.

        Wird an jedem Dokument als `image_tag_at_submit` mitgeschrieben. Der Wert
        beantwortet spaeter Qualitaetsfragen, die man ohne ihn nicht mehr
        beantworten kann: „war das noch das alte Image mit dem Herunterskalier-
        Fehler?" Ohne diese Angabe an der Zeile ist die Frage nach Wochen nicht
        mehr entscheidbar.

        Laeuft ueber GraphQL, weil die REST-Schnittstelle Templates aus einer
        GitHub-Anbindung nicht ausliefert (dort kommt 404 zurueck).

        Returns:
            Der Image-Bezeichner, oder None, wenn das Template ihn nicht ausweist.
            None ist ein gueltiges Ergebnis und kein Fehler — dann steht an der
            Zeile eben nichts, was ehrlicher ist als ein erfundener Wert.

        Kosten: keine.
        """
        daten = await self._graphql(
            "query { myself { endpoints { id templateId template { imageName } } } }",
            "Image-Bezeichner lesen",
        )
        ich = daten.get("myself")
        if not isinstance(ich, dict):
            return None
        endpunkte = ich.get("endpoints")
        if not isinstance(endpunkte, list):
            return None
        for eintrag in endpunkte:
            if not isinstance(eintrag, dict):
                continue
            if eintrag.get("id") != self._settings.endpoint_id:
                continue
            vorlage = eintrag.get("template")
            if not isinstance(vorlage, dict):
                return None
            name = vorlage.get("imageName")
            return name if isinstance(name, str) else None
        # Der Endpoint war nicht in der Liste. Das ist eine Feststellung ueber
        # die Antwort und keine ueber das Image — deshalb None und nicht ein
        # erfundener Wert. Ob der Endpoint noch existiert, beantwortet
        # `endpoint()` mit einer richtigen Fehlermeldung.
        return None
