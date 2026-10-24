"""Der Job-Contract — alles ohne Netz, ueber einen vorgegebenen Transport.

Jeder dieser Tests haelt eine Regel fest, die aus einem bezahlten Fehler
entstanden ist. Sie sind billig und sie sind die einzige Stelle, an der die
Regeln nicht bloss im Kommentar stehen.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mineru_ocr import runpod as runpod_modul
from mineru_ocr.config import Settings
from mineru_ocr.errors import (
    ConfigurationError,
    InsufficientBalanceError,
    JobAufgegebenError,
    ResultLostError,
    TerminalRunPodError,
    TransientRunPodError,
)
from mineru_ocr.runpod import (
    API_BASE,
    RUNNING_STATUSES,
    TERMINAL_STATUSES,
    RunPodClient,
)

SCHLUESSEL = "erfundener-schluessel-nicht-echt"


class Aufzeichnung:
    """Ein Transport, der jede Anfrage mitschreibt und vorgegebene Antworten liefert.

    Die Naht, an der das Netz endet. `RunPodClient` nimmt einen fertigen
    HTTP-Client entgegen — genau dafuer, und deshalb weiss das Modul selbst
    nichts von Tests.
    """

    def __init__(self, antworten: list[httpx.Response] | httpx.Response) -> None:
        self.anfragen: list[httpx.Request] = []
        self._antworten = (
            list(antworten) if isinstance(antworten, list) else [antworten]
        )
        self._nummer = 0

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        # Die letzte Antwort wird wiederholt, wenn mehr gefragt als vorgegeben
        # wurde — so muss ein Test, der die Schleife prueft, nicht abzaehlen.
        nummer = min(self._nummer, len(self._antworten) - 1)
        self._nummer += 1
        return self._antworten[nummer]

    @property
    def nutzlast(self) -> dict[str, Any]:
        """Die JSON-Nutzlast der ERSTEN Anfrage."""
        geparst: dict[str, Any] = json.loads(self.anfragen[0].content)
        return geparst


def _client(
    settings: Settings, transport: Aufzeichnung
) -> tuple[RunPodClient, Aufzeichnung]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return RunPodClient(settings, http=http), transport


def _antwort(daten: dict[str, Any], code: int = 200) -> httpx.Response:
    return httpx.Response(code, json=daten)


class _Uhr:
    """Eine Uhr, die nur der Schlaf der Warteschleife vorstellt.

    Die Zeitbudget-Tests brauchen Zeitspannen, die lang gegen den Abfragetakt
    sind. In echter Zeit hinge ihr Ausgang von der Last des Rechners ab; mit
    dieser Uhr ist er festgelegt.
    """

    def __init__(self) -> None:
        self.jetzt = 0.0
        self._echter_schlaf = asyncio.sleep

    def monotonic(self) -> float:
        return self.jetzt

    async def sleep(self, sekunden: float) -> None:
        self.jetzt += sekunden
        # Eine Schleife, die nie aufgibt, soll den Test scheitern lassen statt
        # ihn aufzuhaengen.
        assert self.jetzt < 10_000.0, "Die Warteschleife gibt nie auf"
        await self._echter_schlaf(0)


@pytest.fixture
def uhr(monkeypatch: pytest.MonkeyPatch) -> _Uhr:
    """Setzt die Uhr der Warteschleife in `runpod` auf eine vorgestellte.

    Nur der Name `time` im Modul wird ersetzt, nicht `time.monotonic` selbst: Das
    wirkte auf die ganze Ereignisschleife.
    """
    eine = _Uhr()
    monkeypatch.setattr(
        runpod_modul, "time", SimpleNamespace(monotonic=eine.monotonic)
    )
    monkeypatch.setattr(runpod_modul.asyncio, "sleep", eine.sleep)
    return eine


@pytest.fixture
def echte_settings(settings: Settings) -> Settings:
    """Einstellungen mit dem erfundenen Schluessel und kurzen Zeitschranken.

    Kurz, damit die Schleifen-Tests nicht in echter Zeit warten: Der
    Abfragetakt steht im Betrieb bei zehn Sekunden, hier bei Bruchteilen einer
    Sekunde. Das PRUEFT die Schleife, ohne sie zu verlangsamen — die Werte
    kommen ohnehin aus den Einstellungen und nicht aus dem Code.
    """
    return dataclasses.replace(
        settings,
        api_key=SCHLUESSEL,
        endpoint_id="l79lebhinxaqjz",
        poll_interval_seconds=0.01,
        job_timeout_seconds=1.0,
    )


# --- Die Nutzlast ---------------------------------------------------------


async def test_nutzlast_hat_kein_formats_feld(echte_settings: Settings) -> None:
    """Die gesendete Nutzlast enthaelt unter keinen Umstaenden `formats`.

    Beide alten Ad-hoc-Skripte hatten `formats: ["markdown"]` gesetzt. Genau
    daraus wurde die Abruf-Regel: alles holen, alles behalten, nie filtern —
    denn dreissig Minuten spaeter gibt es die Antwort nirgends mehr.
    """
    client, transport = _client(
        echte_settings, Aufzeichnung(_antwort({"id": "job-1", "status": "IN_QUEUE"}))
    )
    await client.submit(file_b64="AAAA", backend="vlm-auto-engine", transport="tarball_b64")

    nutzlast = transport.nutzlast
    assert "formats" not in nutzlast
    assert "formats" not in nutzlast["input"]
    # Und auch nicht irgendwo tiefer: Der ganze Text darf das Wort nicht tragen.
    assert "formats" not in transport.anfragen[0].content.decode()


async def test_nutzlast_hat_keine_null_werte(echte_settings: Settings) -> None:
    """Nicht gesetzte Felder werden WEGGELASSEN, nicht als null gesendet.

    Der Worker unterscheidet zwischen „nicht gesetzt" und „ausdruecklich null",
    und null fuehrt zu Verhalten, das wie ein Modellfehler aussieht.
    """
    client, transport = _client(
        echte_settings, Aufzeichnung(_antwort({"id": "job-1"}))
    )
    await client.submit(file_b64="AAAA", backend="vlm-auto-engine", transport="tarball_b64")

    def enthaelt_null(wert: Any) -> bool:
        if wert is None:
            return True
        if isinstance(wert, dict):
            return any(enthaelt_null(w) for w in wert.values())
        if isinstance(wert, list):
            return any(enthaelt_null(w) for w in wert)
        return False

    assert not enthaelt_null(transport.nutzlast)
    assert "null" not in transport.anfragen[0].content.decode()


async def test_seitenbereich_wird_nur_gesendet_wenn_gesetzt(
    echte_settings: Settings,
) -> None:
    """Ohne Angabe stehen `start_page` und `end_page` nicht in der Nutzlast —
    insbesondere wird keine `-1` gesendet."""
    client, ohne = _client(echte_settings, Aufzeichnung(_antwort({"id": "a"})))
    await client.submit(file_b64="A", backend="b", transport="t")
    assert "start_page" not in ohne.nutzlast["input"]
    assert "end_page" not in ohne.nutzlast["input"]

    client2, mit = _client(echte_settings, Aufzeichnung(_antwort({"id": "b"})))
    await client2.submit(
        file_b64="A", backend="b", transport="t", start_page=0, end_page=19
    )
    assert mit.nutzlast["input"]["start_page"] == 0
    assert mit.nutzlast["input"]["end_page"] == 19


async def test_schluessel_steht_nur_im_header(echte_settings: Settings) -> None:
    """Der API-Schluessel taucht in keiner URL auf, in keinem Nutzlast-Feld und
    in keiner Ausnahme-Meldung."""
    client, transport = _client(
        echte_settings,
        Aufzeichnung(httpx.Response(400, text="Payload too large")),
    )
    with pytest.raises(TerminalRunPodError) as fehler:
        await client.submit(file_b64="A", backend="b", transport="t")

    anfrage = transport.anfragen[0]
    assert anfrage.headers["Authorization"] == f"Bearer {SCHLUESSEL}"
    assert SCHLUESSEL not in str(anfrage.url)
    assert SCHLUESSEL not in anfrage.content.decode()
    assert SCHLUESSEL not in str(fehler.value)


async def test_abbruch_geht_an_cancel_mit_der_kennung(
    echte_settings: Settings,
) -> None:
    """`cancel` schickt `POST /cancel/<job-id>` an den Endpoint — und der Schluessel
    reist auch dabei nur im Header.

    Aufgegebene Jobs laufen bei RunPod sonst weiter und kosten Geld. Die Kennung
    steht im Pfad und nirgends sonst; eine Nutzlast braucht der Abbruch nicht.
    """
    client, transport = _client(
        echte_settings,
        Aufzeichnung(_antwort({"id": "job-9", "status": "CANCELLED"})),
    )
    antwort = await client.cancel("job-9")

    assert antwort["status"] == "CANCELLED"
    assert len(transport.anfragen) == 1
    anfrage = transport.anfragen[0]
    assert anfrage.method == "POST"
    assert str(anfrage.url) == f"{API_BASE}/l79lebhinxaqjz/cancel/job-9"
    assert anfrage.headers["Authorization"] == f"Bearer {SCHLUESSEL}"
    assert SCHLUESSEL not in str(anfrage.url)
    assert SCHLUESSEL not in anfrage.content.decode()


def test_es_gibt_kein_runsync() -> None:
    """Der Zugang bietet keine Methode, die `/runsync` aufruft.

    Nach einer Ruhephase braucht der Endpoint rund achteinhalb Minuten zum
    Kaltstart, `/runsync` wartet sechzig Sekunden — man bekaeme eine
    Zeitueberschreitung auf einen Job, der laeuft und gleich bezahlt ist. Eine
    Methode, die es nicht gibt, kann niemand versehentlich benutzen.

    Geprueft wird am QUELLTEXT des Moduls und nicht nur an den Methodennamen:
    Eine Methode koennte anders heissen und trotzdem `/runsync` aufrufen.
    """
    import mineru_ocr.runpod as modul

    quelle = inspect.getsource(modul)
    # Im Vorspann steht das Wort — dort erklaert es, warum es die Methode nicht
    # gibt. Was es NICHT geben darf, ist ein Aufruf.
    assert "/runsync" not in quelle.replace("`/runsync`", "")
    assert not any(name.endswith("runsync") for name in dir(RunPodClient))


# --- Die Fehlerzuordnung --------------------------------------------------


async def test_404_beendet_das_abfragen_aber_nicht_das_dokument(
    echte_settings: Settings,
) -> None:
    """HTTP 404 beendet das ABFRAGEN endgueltig — das DOKUMENT aber nicht.

    Der Test hiess frueher `test_404_beim_abfragen_ist_endgueltig` und sein
    Docstring sagte „kein Weiterfragen, kein Retry". Geprueft hat er nur, DASS
    geworfen wird; ueber die Terminalitaet stand nichts im Koerper. Und die
    Einstufung ist das Gegenteil: `ResultLostError` erbt von
    `TransientRunPodError` und gilt als wiederholbar — das Dokument laeuft neu
    und wird dabei erneut bezahlt.

    Wer nach dem alten Namen ging, hielt die Doppelbezahlung fuer
    ausgeschlossen. Deshalb prueft dieser Test jetzt BEIDE Haelften, und sein
    Name nennt sie.
    """
    from mineru_ocr.errors import FailureKind, classify_failure, should_run_again

    client, _ = _client(
        echte_settings, Aufzeichnung(httpx.Response(404, text="not found"))
    )
    with pytest.raises(ResultLostError) as gefangen:
        await client.status("job-1")

    # Das Abfragen ist vorbei — `await_result` bricht daran ab, statt
    # weiterzupollen. Das ist die Haelfte, die der alte Name meinte.
    # Und die andere Haelfte, die er verschwieg:
    assert classify_failure(gefangen.value) is FailureKind.RETRYABLE
    assert should_run_again(gefangen.value) is True


async def test_402_ergibt_kontostands_ausnahme(echte_settings: Settings) -> None:
    """HTTP 402 beim Absenden ergibt InsufficientBalanceError, nicht einen
    gewoehnlichen Fehler."""
    client, _ = _client(
        echte_settings, Aufzeichnung(httpx.Response(402, text="Insufficient Balance"))
    )
    with pytest.raises(InsufficientBalanceError):
        await client.submit(file_b64="A", backend="b", transport="t")


async def test_401_und_403_sind_konfigurationsfehler(
    echte_settings: Settings,
) -> None:
    """Ein untauglicher Schluessel ergibt ConfigurationError — Wiederholen
    macht ihn nicht gueltig.

    Und die Meldung nennt die ZWEITE Ursache mit: Auch eine URL auf einen
    geloeschten Endpoint antwortet mit 401. Das ist im Betrieb schon
    vorgekommen, und wer es nicht weiss, tauscht stundenlang Schluessel aus.
    """
    for code in (401, 403):
        client, _ = _client(
            echte_settings, Aufzeichnung(httpx.Response(code, text="invalid api key"))
        )
        with pytest.raises(ConfigurationError) as fehler:
            await client.submit(file_b64="A", backend="b", transport="t")
        assert "l79lebhinxaqjz" in str(fehler.value)


async def test_5xx_ist_wiederholbar(echte_settings: Settings) -> None:
    """HTTP 5xx ergibt TransientRunPodError."""
    for code in (500, 502, 503):
        client, _ = _client(
            echte_settings, Aufzeichnung(httpx.Response(code, text="boom"))
        )
        with pytest.raises(TransientRunPodError):
            await client.submit(file_b64="A", backend="b", transport="t")


async def test_transportfehler_ist_wiederholbar(echte_settings: Settings) -> None:
    """Ein Netzwackeln ergibt TransientRunPodError, keinen endgueltigen Fehler."""

    def wirft(anfrage: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Verbindung abgelehnt", request=anfrage)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wirft))
    client = RunPodClient(echte_settings, http=http)
    with pytest.raises(TransientRunPodError):
        await client.submit(file_b64="A", backend="b", transport="t")


async def test_antwort_ohne_kennung_wirft_mit_voller_antwort(
    echte_settings: Settings,
) -> None:
    """Fehlt die Job-Kennung, wird die VOLLE Antwort mitgegeben.

    Der schlimmste denkbare Zustand: Der Job kann angenommen und damit bezahlt
    sein, und wir kennen seine Kennung nicht. Die volle Antwort ist dann das
    Einzige, womit man ihn noch suchen koennte.
    """
    client, _ = _client(
        echte_settings, Aufzeichnung(_antwort({"status": "IN_QUEUE", "irgendwas": 7}))
    )
    with pytest.raises(TerminalRunPodError) as fehler:
        await client.submit(file_b64="A", backend="b", transport="t")
    assert "irgendwas" in str(fehler.value)
    assert "bezahlt" in str(fehler.value)


# --- Die Warteschleife ----------------------------------------------------


async def test_abwarten_liefert_auch_gescheiterte_antworten(
    echte_settings: Settings,
) -> None:
    """`await_result` gibt eine FAILED-Antwort ZURUECK, statt zu werfen.

    Wuerde hier geworfen, waere die bezahlte Fehlerantwort verloren — dabei ist
    gerade sie das, was man spaeter ansehen will.
    """
    client, _ = _client(
        echte_settings,
        Aufzeichnung(_antwort({"status": "FAILED", "error": "OOM", "id": "j"})),
    )
    antwort = await client.await_result("j")
    assert antwort["status"] == "FAILED"
    assert antwort["error"] == "OOM"


async def test_abwarten_endet_bei_terminalem_zustand(
    echte_settings: Settings,
) -> None:
    """Jeder Zustand aus TERMINAL_STATUSES beendet die Schleife."""
    for zustand in TERMINAL_STATUSES:
        client, transport = _client(
            echte_settings, Aufzeichnung(_antwort({"status": zustand}))
        )
        antwort = await client.await_result("j")
        assert antwort["status"] == zustand
        # Genau EINE Abfrage: terminal heisst terminal.
        assert len(transport.anfragen) == 1


async def test_running_gilt_als_laufend(echte_settings: Settings) -> None:
    """`RUNNING` beendet die Schleife NICHT.

    Der Zustand wurde im Betrieb beobachtet, obwohl die Dokumentation nur
    `IN_PROGRESS` nennt. Ein unbekannter Zustand wuerde als Fehler behandelt und
    ein laufender Job abgebrochen.
    """
    assert "RUNNING" in RUNNING_STATUSES
    client, transport = _client(
        echte_settings,
        Aufzeichnung(
            [
                _antwort({"status": "IN_QUEUE"}),
                _antwort({"status": "RUNNING"}),
                _antwort({"status": "IN_PROGRESS"}),
                _antwort({"status": "COMPLETED", "output": {"ok": True}}),
            ]
        ),
    )
    antwort = await client.await_result("j")
    assert antwort["status"] == "COMPLETED"
    assert len(transport.anfragen) == 4


async def test_unbekannter_zustand_bricht_nicht_ab(echte_settings: Settings) -> None:
    """Ein Zustand, den niemand kennt, beendet die Schleife NICHT.

    Beides waere teuer: ihn als terminal zu verbuchen wuerfe einen laufenden,
    bezahlten Job weg; ihn als Fehler zu werfen ebenso. Weiterfragen kostet
    nichts, und die Zeitschranke begrenzt es ohnehin.
    """
    client, transport = _client(
        echte_settings,
        Aufzeichnung(
            [
                _antwort({"status": "SOMETHING_NEW"}),
                _antwort({"status": "COMPLETED"}),
            ]
        ),
    )
    antwort = await client.await_result("j")
    assert antwort["status"] == "COMPLETED"
    assert len(transport.anfragen) == 2


async def test_netzwackeln_beendet_das_warten_nicht(
    echte_settings: Settings,
) -> None:
    """Eine fehlgeschlagene Abfrage bricht das Warten NICHT ab.

    Der wichtigste Einzelfall der Schleife: Ein Netzwackeln heisst nicht, dass
    das Ergebnis weg ist — es liegt auf der Gegenseite noch bis zu dreissig
    Minuten bereit. Wer hier abbricht, wirft ein bezahltes, noch abholbares
    Ergebnis weg und bezahlt es beim naechsten Anlauf ein zweites Mal.
    """
    client, transport = _client(
        echte_settings,
        Aufzeichnung(
            [
                httpx.Response(503, text="kurz weg"),
                httpx.Response(500, text="immer noch"),
                _antwort({"status": "COMPLETED", "output": {"ok": True}}),
            ]
        ),
    )
    antwort = await client.await_result("j")
    assert antwort["status"] == "COMPLETED"
    assert len(transport.anfragen) == 3


async def test_verlorenes_ergebnis_beendet_das_warten_sofort(
    echte_settings: Settings,
) -> None:
    """Nur HTTP 404 beendet die Schleife sofort — dann ist wirklich nichts mehr
    da, und weiter abzufragen kann nie mehr etwas liefern."""
    client, transport = _client(
        echte_settings, Aufzeichnung(httpx.Response(404, text="weg"))
    )
    with pytest.raises(ResultLostError):
        await client.await_result("j")
    assert len(transport.anfragen) == 1


async def test_zeitueberschreitung_ist_wiederholbar(
    echte_settings: Settings,
) -> None:
    """Ueberschreitet die Abfrageschleife `job_timeout_seconds`, kommt
    TransientRunPodError."""
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=0.2, poll_interval_seconds=0.01
    )
    client, _ = _client(knapp, Aufzeichnung(_antwort({"status": "IN_PROGRESS"})))
    with pytest.raises(TransientRunPodError) as fehler:
        await client.await_result("j")
    assert "Wiederholbar" in str(fehler.value)


async def test_aufgegebener_job_wird_vor_der_wiederholung_abgebrochen(
    echte_settings: Settings,
) -> None:
    """Gibt das Paket einen Job nach Ablauf seines Zeitbudgets auf, geht
    `/cancel` fuer GENAU diesen Job hinaus — und zwar bevor die Ausnahme den
    Aufrufer erreicht, der wiederholt.

    Sonst liefe der aufgegebene Job bei RunPod weiter und kostete Geld, waehrend
    der neue Versuch dasselbe Teilstueck ein zweites Mal bezahlt.
    """
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=0.1, poll_interval_seconds=0.01
    )
    client, transport = _client(
        knapp, Aufzeichnung(_antwort({"status": "IN_PROGRESS"}))
    )
    with pytest.raises(TransientRunPodError):
        await client.await_result("job-7")

    abbrueche = [a for a in transport.anfragen if "/cancel/" in str(a.url)]
    assert len(abbrueche) == 1
    assert abbrueche[0].method == "POST"
    assert str(abbrueche[0].url).endswith("/cancel/job-7")
    # Der Abbruch ist die LETZTE Anfrage: nach ihm wird nichts mehr abgefragt.
    assert transport.anfragen[-1] is abbrueche[0]
    assert SCHLUESSEL not in str(abbrueche[0].url)


async def test_misslungener_abbruch_verdeckt_die_zeitueberschreitung_nicht(
    echte_settings: Settings,
) -> None:
    """Scheitert das `/cancel` selbst, kommt trotzdem die wiederholbare
    Zeitueberschreitung heraus — mit dem Grund des Abbruchs in der Meldung.

    Der Abbruch ist ein Versuch, Geld zu sparen. Wuerde sein Fehler den Grund
    verdecken, ginge die Wiedervorlage verloren oder liefe unter einem falschen
    Fehlertyp (ein 401 wuerde den Job endgueltig scheitern lassen).
    """

    def antwort(anfrage: httpx.Request) -> httpx.Response:
        if "/cancel/" in str(anfrage.url):
            return httpx.Response(401, text="kein Zugang")
        return _antwort({"status": "IN_PROGRESS"})

    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=0.1, poll_interval_seconds=0.01
    )
    client = RunPodClient(
        knapp, http=httpx.AsyncClient(transport=httpx.MockTransport(antwort))
    )
    with pytest.raises(TransientRunPodError) as fehler:
        await client.await_result("job-7")

    assert "Wiederholbar" in str(fehler.value)
    assert "abbrechen" in str(fehler.value)


async def test_zeitbudget_laeuft_nicht_in_der_warteschlange(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Warteschlange und Kaltstart zaehlen nicht zum Zeitbudget.

    Mit `/cancel` bei Ablauf wuerde ein Job, der hinter einem grossen Lauf
    wartet, sonst immer wieder abgebrochen und neu eingereiht — und jedes Mal
    neu bezahlt. Hier steht der Job fuenfmal laenger in der Schlange, als das
    Budget erlaubt, und kommt trotzdem durch.
    """
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=0.2, poll_interval_seconds=0.01
    )
    client, transport = _client(
        knapp,
        Aufzeichnung(
            [_antwort({"status": "IN_QUEUE"})] * 100
            + [_antwort({"status": "IN_PROGRESS"})] * 3
            + [_antwort({"status": "COMPLETED", "output": {"ok": True}})]
        ),
    )
    antwort = await client.await_result("j")

    assert antwort["status"] == "COMPLETED"
    assert len(transport.anfragen) == 104
    assert not any("/cancel/" in str(a.url) for a in transport.anfragen)


async def test_zeitbudget_beginnt_bei_in_progress(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Das Budget laeuft ab dem Moment, in dem RunPod `IN_PROGRESS` meldet.

    Zwanzig Sekunden in der Schlange, dann ein Budget von 10 Sekunden: Die
    Ausnahme kommt nach 30 Sekunden. Zaehlte die Schlange mit, kaeme sie nach 10.
    """
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=10.0, poll_interval_seconds=1.0
    )
    client, _ = _client(
        knapp,
        Aufzeichnung(
            [_antwort({"status": "IN_QUEUE"})] * 20
            + [_antwort({"status": "IN_PROGRESS"})]
        ),
    )
    with pytest.raises(JobAufgegebenError):
        await client.await_result("j")
    assert uhr.jetzt == 30.0


async def test_zeitbudget_zaehlt_nur_die_zeit_in_progress_ueber_alle_laeufe(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Geht der Job nach `IN_PROGRESS` zurueck in die Schlange (etwa weil ein
    Worker wegbrach), zaehlt die Schlangenzeit nicht mit, die schon verbrauchte
    Laufzeit aber bleibt verbraucht.

    Begaenne das Budget bei jedem Lauf neu, kaeme ein Job, den ein Worker nach
    dem anderen abstuerzen laesst, nie an die Grenze: Jeder Lauf wuerde bezahlt,
    und das Paket gaebe nie auf.
    """
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=10.0, poll_interval_seconds=1.0
    )
    client, transport = _client(
        knapp,
        Aufzeichnung(
            [_antwort({"status": "IN_PROGRESS"})] * 6
            + [_antwort({"status": "IN_QUEUE"})] * 100
            + [_antwort({"status": "IN_PROGRESS"})]
        ),
    )
    with pytest.raises(JobAufgegebenError):
        await client.await_result("j")

    # 6 s gelaufen, 100 s gewartet, dann die restlichen 4 s des Budgets.
    assert uhr.jetzt == 110.0
    assert sum("/cancel/" in str(a.url) for a in transport.anfragen) == 1


async def test_wartezeit_in_der_schlange_nach_einem_lauf_verbraucht_das_budget_nicht(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Der Job laeuft kurz, steht lange in der Schlange und laeuft dann zu Ende."""
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=10.0, poll_interval_seconds=1.0
    )
    client, transport = _client(
        knapp,
        Aufzeichnung(
            [_antwort({"status": "IN_PROGRESS"})] * 5
            + [_antwort({"status": "IN_QUEUE"})] * 100
            + [_antwort({"status": "IN_PROGRESS"})] * 3
            + [_antwort({"status": "COMPLETED", "output": {"ok": True}})]
        ),
    )
    antwort = await client.await_result("j")

    assert antwort["status"] == "COMPLETED"
    assert not any("/cancel/" in str(a.url) for a in transport.anfragen)


@pytest.mark.parametrize("zustand", ["IN_PROGRESS", "RUNNING", "STARTING"])
async def test_jeder_zustand_ausser_der_schlange_laesst_das_budget_laufen(
    echte_settings: Settings, uhr: _Uhr, zustand: str
) -> None:
    """`IN_PROGRESS`, das beobachtete `RUNNING` und ein unbekannter Zustand
    zaehlen als Arbeit: Sonst haelte ein unbekanntes Wort fuer immer einen Platz
    im Daemon und kostete weiter, ohne dass je abgebrochen wuerde."""
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=10.0, poll_interval_seconds=1.0
    )
    client, transport = _client(knapp, Aufzeichnung(_antwort({"status": zustand})))
    with pytest.raises(JobAufgegebenError):
        await client.await_result("j")

    assert sum("/cancel/" in str(a.url) for a in transport.anfragen) == 1


async def test_dauerhaft_fehlschlagende_abfragen_werden_aufgegeben(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Fallen die Abfragen vor dem ersten `IN_PROGRESS` dauerhaft aus (5xx), gibt
    das Paket nach dem Zeitbudget auf und bricht ab.

    Das Budget laeuft erst ab `IN_PROGRESS`; ohne eine eigene Grenze fuer
    fehlschlagende Abfragen hielte ein Ausfall von RunPod jeden Platz im Daemon
    ohne Ende besetzt.
    """
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=100.0, poll_interval_seconds=1.0
    )
    client, transport = _client(
        knapp, Aufzeichnung(_antwort({"error": "kaputt"}, code=503))
    )
    with pytest.raises(JobAufgegebenError) as fehler:
        await client.await_result("j")

    assert "Wiederholbar" in str(fehler.value)
    assert sum("/cancel/" in str(a.url) for a in transport.anfragen) == 1


async def test_ein_gelungenes_abfragen_setzt_die_fehlerfrist_zurueck(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Ein kurzer Ausfall mitten in der Schlange gibt den Job nicht auf."""
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=100.0, poll_interval_seconds=1.0
    )
    ausfall = _antwort({"error": "kaputt"}, code=503)
    client, transport = _client(
        knapp,
        Aufzeichnung(
            [_antwort({"status": "IN_QUEUE"})] * 3
            + [ausfall] * 5
            + [_antwort({"status": "IN_QUEUE"})] * 3
            + [ausfall] * 5
            + [_antwort({"status": "COMPLETED", "output": {"ok": True}})]
        ),
    )
    antwort = await client.await_result("j")

    assert antwort["status"] == "COMPLETED"
    assert not any("/cancel/" in str(a.url) for a in transport.anfragen)


async def test_ein_job_der_im_letzten_takt_fertig_wird_wird_nicht_abgebrochen(
    echte_settings: Settings, uhr: _Uhr
) -> None:
    """Vor dem Aufgeben fragt das Paket ein letztes Mal ab.

    Wird der Job im letzten Abfragetakt fertig, ist er bezahlt. Ihn abzubrechen
    und neu abzusenden zahlte ihn ein zweites Mal.
    """
    knapp = dataclasses.replace(
        echte_settings, job_timeout_seconds=3.0, poll_interval_seconds=1.0
    )
    client, transport = _client(
        knapp,
        Aufzeichnung(
            [_antwort({"status": "IN_PROGRESS"})] * 3
            + [_antwort({"status": "COMPLETED", "output": {"ok": True}})]
        ),
    )
    antwort = await client.await_result("j")

    assert antwort["status"] == "COMPLETED"
    assert not any("/cancel/" in str(a.url) for a in transport.anfragen)


@pytest.mark.parametrize(
    ("code", "erwartet"),
    [
        (404, ResultLostError),
        (401, ConfigurationError),
        (403, ConfigurationError),
        (500, TransientRunPodError),
        (400, TerminalRunPodError),
    ],
)
async def test_abbruch_uebersetzt_fehlerantworten_wie_die_anderen_aufrufe(
    echte_settings: Settings, code: int, erwartet: type[Exception]
) -> None:
    """`cancel` wirft die Fehlerklassen, die sein Docstring verspricht."""
    client, _ = _client(echte_settings, Aufzeichnung(_antwort({"error": "x"}, code)))
    with pytest.raises(erwartet):
        await client.cancel("job-9")


async def test_abbruch_ueber_ein_totes_netz_wirft_wiederholbar(
    echte_settings: Settings,
) -> None:
    def tot(anfrage: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Netz weg", request=anfrage)

    client = RunPodClient(
        echte_settings, http=httpx.AsyncClient(transport=httpx.MockTransport(tot))
    )
    with pytest.raises(TransientRunPodError):
        await client.cancel("job-9")


async def test_abbruch_wird_durchgereicht(echte_settings: Settings) -> None:
    """CancelledError in der Warteschleife wird nicht gefangen und nicht
    verwandelt — sonst liefe der Task beim Herunterfahren weiter.

    Geprueft, indem die Koroutine mitten in der Warteschleife abgebrochen wird.
    Kaeme statt `CancelledError` etwas anderes heraus, wuerde Procrastinate den
    Job nicht als `aborted` verbuchen, und der Daemon fuehre nicht herunter.
    """
    langsam = dataclasses.replace(
        echte_settings, poll_interval_seconds=10.0, job_timeout_seconds=600.0
    )
    client, _ = _client(langsam, Aufzeichnung(_antwort({"status": "IN_QUEUE"})))

    aufgabe = asyncio.create_task(client.await_result("j"))
    await asyncio.sleep(0.05)
    aufgabe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await aufgabe


# --- Konto und Endpoint ---------------------------------------------------


async def test_worker_liste_ist_einzeln(echte_settings: Settings) -> None:
    """`workers` liefert Eintraege je Worker, nicht die zusammengefassten
    Zaehler des Endpoints (die luegen).

    Die Form stammt aus dem konservierten Schnappschuss vom 05.08.2026: Der
    Endpoint traegt `workersMax`/`workersMin`/`workersStandby` als Zahlen UND
    eine `workers`-Liste mit je einem Eintrag. Dieser Test haelt fest, dass wir
    die Liste nehmen und nicht die Zahlen.
    """
    client, _ = _client(
        echte_settings,
        Aufzeichnung(
            _antwort(
                {
                    "id": "l79lebhinxaqjz",
                    "workersMax": 10,
                    "workersMin": 0,
                    "workersStandby": 3,
                    "workers": [
                        {"id": "7vbp39c71bllhb", "desiredStatus": "EXITED"},
                        {"id": "abc123def456gh", "desiredStatus": "RUNNING"},
                    ],
                }
            )
        ),
    )
    liste = await client.workers()
    assert [w["id"] for w in liste] == ["7vbp39c71bllhb", "abc123def456gh"]


async def test_leere_worker_liste_ist_kein_fehler(echte_settings: Settings) -> None:
    """Kein laufender Worker ist der Normalzustand bei einem Endpoint, der auf
    null herunterskaliert."""
    client, _ = _client(
        echte_settings, Aufzeichnung(_antwort({"id": "x", "workers": []}))
    )
    assert await client.workers() == []


async def test_fehlendes_workers_feld_ist_kein_leerer_endpoint(
    echte_settings: Settings,
) -> None:
    """Kein `workers`-Feld heisst NICHT „keine Worker".

    Der Unterschied darf nicht stillschweigend zu einer leeren Liste werden,
    denn daraus wuerde die Aussage „es laeuft keiner" — und die trifft man dann
    auf einer Erfindung.
    """
    client, _ = _client(
        echte_settings, Aufzeichnung(_antwort({"id": "x", "workersMax": 10}))
    )
    with pytest.raises(TerminalRunPodError):
        await client.workers()


async def test_kontostand_wird_gelesen(echte_settings: Settings) -> None:
    """Der Kontostand kommt aus `myself.clientBalance`.

    Die Form ist am konservierten Schnappschuss `user-snapshot.json` abgelesen
    (Quelle `runpodctl user`): `clientBalance`, `currentSpendPerHr`,
    `spendLimit`.
    """
    client, transport = _client(
        echte_settings,
        Aufzeichnung(
            _antwort(
                {
                    "data": {
                        "myself": {
                            "clientBalance": 5.1889494134,
                            "currentSpendPerHr": 0,
                            "spendLimit": 80,
                        }
                    }
                }
            )
        ),
    )
    assert await client.balance() == pytest.approx(5.1889494134)
    # Und der browser-taugliche User-Agent ist dabei — ohne ihn antwortet der
    # Dienst nicht (gemessen, nicht vermutet).
    assert "Mozilla" in transport.anfragen[0].headers["User-Agent"]


async def test_kontostand_fehlschlag_ist_nicht_leeres_konto(
    echte_settings: Settings,
) -> None:
    """Eine gescheiterte Kontostands-Abfrage wirft und liefert nicht die Null.

    Die Verwechslung waere in beide Richtungen teuer: die Arbeit wegen eines
    Netzwackelns anhalten, oder bei leerem Konto weiterlaufen.
    """
    # Antwort ohne das Feld — nicht null, sondern gar nicht da.
    client, _ = _client(
        echte_settings, Aufzeichnung(_antwort({"data": {"myself": {"spendLimit": 80}}}))
    )
    with pytest.raises(TransientRunPodError) as fehler:
        await client.balance()
    assert "NICHT ein leeres Konto" in str(fehler.value)


async def test_graphql_fehler_wird_nicht_als_ergebnis_gelesen(
    echte_settings: Settings,
) -> None:
    """GraphQL antwortet auf Fehler mit HTTP 200 und einem `errors`-Feld.

    Wer nur den Antwortcode prueft, haelt eine Fehlerantwort fuer ein Ergebnis
    und liest dann `None` als „Kontostand null".
    """
    client, _ = _client(
        echte_settings,
        Aufzeichnung(_antwort({"errors": [{"message": "unauthorized"}], "data": None})),
    )
    with pytest.raises(TerminalRunPodError) as fehler:
        await client.balance()
    assert "unauthorized" in str(fehler.value)


async def test_image_bezeichner_wird_gelesen(echte_settings: Settings) -> None:
    """Der Image-Bezeichner kommt ueber GraphQL, nicht ueber REST.

    Die REST-Schnittstelle liefert Templates aus einer GitHub-Anbindung nicht
    aus (dort kommt 404). Nachpruef-Ort laut Betriebshandbuch ist
    `myself { endpoints { template { imageName } } }`.
    """
    client, _ = _client(
        echte_settings,
        Aufzeichnung(
            _antwort(
                {
                    "data": {
                        "myself": {
                            "endpoints": [
                                {"id": "andererendpoint", "template": {"imageName": "x"}},
                                {
                                    "id": "l79lebhinxaqjz",
                                    "templateId": "5wv9rko6zv",
                                    "template": {
                                        "imageName": "registry.runpod.net/"
                                        "constantinhvber-mineru-runpod-main-"
                                        "dockerfile:17d4955b2"
                                    },
                                },
                            ]
                        }
                    }
                }
            )
        ),
    )
    marke = await client.image_tag()
    assert marke == (
        "registry.runpod.net/constantinhvber-mineru-runpod-main-dockerfile:17d4955b2"
    )


async def test_fehlender_image_bezeichner_ergibt_none(
    echte_settings: Settings,
) -> None:
    """Kein Bezeichner ist ein gueltiges Ergebnis und kein Fehler.

    Dann steht an der Zeile eben nichts — ehrlicher als ein erfundener Wert.
    """
    client, _ = _client(
        echte_settings, Aufzeichnung(_antwort({"data": {"myself": {"endpoints": []}}}))
    )
    assert await client.image_tag() is None


# --- Die Kosten-Regel -----------------------------------------------------


def test_kein_kommando_veraendert_bezahlte_ressourcen() -> None:
    """Die Klasse hat keine Methode, die `workersMax` setzt, ein Template
    aendert oder einen Endpoint anlegt — nicht abgeschaltet, sondern nicht
    vorhanden.

    Geprueft an zwei Seiten: an den oeffentlichen Methodennamen und am
    Quelltext. Der zweite Teil faengt, was der erste nicht sieht — eine
    harmlos benannte Methode, die eine veraendernde Anfrage stellt.
    """
    oeffentlich = [n for n in dir(RunPodClient) if not n.startswith("_")]
    assert sorted(oeffentlich) == [
        "account",
        "aclose",
        "await_result",
        "balance",
        "cancel",
        "endpoint",
        "image_tag",
        "status",
        "submit",
        "workers",
    ]

    import mineru_ocr.runpod as modul

    quelle = inspect.getsource(modul)
    # Die einzige veraendernde HTTP-Methode, die vorkommen darf, ist das POST
    # auf `/run`, auf `/cancel/<job-id>` und auf GraphQL — Einreichen und
    # Abbrechen eines eigenen Jobs sowie Lesen, nie ein Eingriff in Endpoint,
    # Template oder Worker.
    for verboten in ["PATCH", "DELETE", "PUT", "workersMax=", "saveEndpoint", "mutation"]:
        assert verboten not in quelle, verboten


def test_der_job_pfad_ist_der_kanonische(echte_settings: Settings) -> None:
    """Die URLs stehen so im Betriebshandbuch — und die Wirtsnamen sind drei
    verschiedene.

    `api.runpod.ai` ist die Job-Schnittstelle, `rest.runpod.io` die Verwaltung,
    `api.runpod.io` das GraphQL. Sie unterscheiden sich um einen Buchstaben, und
    die Verwechslung erzeugt ein HTTP 401, das nach einem falschen Schluessel
    aussieht und keiner ist.
    """
    assert API_BASE == "https://api.runpod.ai/v2"
