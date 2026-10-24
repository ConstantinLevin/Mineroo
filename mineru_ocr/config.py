"""Alle Vorgaben an EINER Stelle — samt der Herleitung, aus der jede Zahl stammt.

Warum das ein eigenes Modul ist und nicht ueber die Module verstreut: Eine Zahl,
die an zwei Stellen steht, wird beim naechsten Mal an einer Stelle geaendert. Und
eine Zahl ohne ihre Herleitung ist beim naechsten Lesen nicht mehr von einem
Griff ins Blaue zu unterscheiden — dann traut sich niemand, sie anzufassen, und
niemand kann sagen, ob sie noch stimmt. Deshalb steht an jedem Feld, WORAUS es
folgt und WAS es kippen wuerde.

Die Schwellenwerte hier sind zum groesseren Teil hergeleitet und zum kleineren
Teil gesetzt. Wo etwas gesetzt und nicht gemessen ist, steht es dabei — Plan 12
fuehrt diese Punkte als offene Nachmessungen.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .errors import ConfigurationError

DEFAULT_CONFIG_PATH: Final = Path.home() / ".mineru_ocr" / "config.toml"
"""Die Konfigurationsdatei, wenn nichts anderes gesagt ist. Fehlt sie, ist das
kein Fehler: Wer alles ueber Umgebungsvariablen einstellt, braucht keine."""

ENV_PREFIX: Final = "MINERU_OCR_"
"""Vorsatz der Umgebungsvariablen. Jeder Schluessel der Konfigurationsdatei hat
eine: `endpoint_id` ↔ `MINERU_OCR_ENDPOINT_ID`."""

ENV_CONFIG_PATH: Final = "MINERU_OCR_CONFIG"
"""Umgebungsvariable, die auf eine andere Konfigurationsdatei zeigt."""

ZAHL_SCHLUESSEL: Final = (
    "workers_min",
    "workers_max",
    "idle_timeout",
    "execution_timeout_ms",
)
"""Die Sollwerte des Endpoints, die Ganzzahlen sind."""

DATEI_SCHLUESSEL: Final = frozenset(
    {
        "endpoint_id",
        "db_dsn",
        "expected_image_tag",
        "allowed_data_centers",
        "endpoint_env",
        *ZAHL_SCHLUESSEL,
    }
)
"""Die Schluessel, die die Konfigurationsdatei kennt. Ein anderer ist ein Fehler
und kein stilles Ignorieren: Ein Tippfehler im Namen waere sonst ein Wert, der da
steht und nirgends wirkt. Der API-Schluessel gehoert ausdruecklich NICHT hierher;
er bleibt in `~/.runpod/config.toml`."""

EWR_RECHENZENTREN: Final = (
    "EU-CZ-1",
    "EU-FR-1",
    "EU-NL-1",
    "EU-RO-1",
    "EU-SE-1",
    "EUR-IS-1",
    "EUR-IS-2",
    "EUR-NO-1",
)
"""Die Rechenzentren im EWR, in denen die DSGVO gilt: EU-Staaten plus Island und
Norwegen (`EUR-IS-*`, `EUR-NO-*`). Die Kennungen sind der RunPod-Dokumentation
vom 25.10.2026 entnommen (Seiten zu Global Networking, S3-API und SDK-
Parametern), nicht am Konto abgefragt; sie kann veralten, wenn RunPod
Rechenzentren hinzufuegt. Deshalb ist sie ein Sollwert der Konfiguration
(`allowed_data_centers`) und keine Mauer im Code: Wer ein neues EWR-Rechenzentrum
zulaesst, tut das in seiner Konfiguration."""

DEFAULT_ENDPOINT_ENV: Final[Mapping[str, str | None]] = {
    "MINERU_MODEL_SOURCE": "local",
    "MINERU_MAX_CONCURRENCY": "1",
    "MINERU_VIRTUAL_VRAM_SIZE": None,
}
"""Die Endpoint-Variablen, auf die es ankommt, mit ihrem Sollwert.

`MINERU_MODEL_SOURCE=local`: Der Worker laedt seine Modelle offline aus dem Image
und holt nichts von Dritten. `MINERU_MAX_CONCURRENCY=1`: ein Teilstueck je Worker
zugleich, siehe `ocr_concurrency`. `MINERU_VIRTUAL_VRAM_SIZE` ist MinerUs Weg, den
GPU-Speicheranteil von vLLM einzustellen; ihr Wert kommt aus der Messung, die
noch aussteht. `None` heisst: Sollwert noch offen — der Preflight berichtet dann
den Ist-Wert und bewertet ihn als nicht pruefbar, statt ihn zu uebergehen."""

RUNPOD_CONFIG_PATH: Final = Path.home() / ".runpod" / "config.toml"
"""Der im Projekt bewaehrte Ort des API-Schluessels, Feld `apikey`. Wird
gelesen, nie geschrieben."""

MASKE: Final = "***"
"""Was anstelle des Schluessels in jeder Ausgabe steht."""


def _schluessel_aus_datei() -> str:
    """Liest den API-Schluessel aus `~/.runpod/config.toml`, Feld `apikey`.

    Raises:
        ConfigurationError: Datei fehlt, ist unlesbar, ist kein gueltiges TOML,
            hat kein Feld `apikey`, oder das Feld ist leer. Jeder dieser Faelle
            bekommt eine eigene Meldung — „Konfigurationsfehler" allein hilft
            beim Suchen niemandem, und der Pfad gehoert in die Meldung, weil er
            die erste Stelle ist, an der man nachsieht.
    """
    if not RUNPOD_CONFIG_PATH.exists():
        raise ConfigurationError(
            f"Kein API-Schluessel: weder uebergeben noch in {RUNPOD_CONFIG_PATH} "
            f"(Datei existiert nicht)."
        )
    try:
        inhalt = RUNPOD_CONFIG_PATH.read_bytes()
    except OSError as fehler:
        raise ConfigurationError(
            f"{RUNPOD_CONFIG_PATH} ist nicht lesbar: {fehler}"
        ) from fehler
    try:
        daten = tomllib.loads(inhalt.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as fehler:
        raise ConfigurationError(
            f"{RUNPOD_CONFIG_PATH} ist kein gueltiges TOML: {fehler}"
        ) from fehler

    schluessel = daten.get("apikey")
    if not isinstance(schluessel, str) or not schluessel.strip():
        raise ConfigurationError(
            f"{RUNPOD_CONFIG_PATH} hat kein nutzbares Feld 'apikey'."
        )
    return schluessel

def _konfigurationsdatei(pfad: Path, *, ausdruecklich: bool) -> dict[str, Any]:
    """Liest die Konfigurationsdatei dieses Pakets.

    Args:
        pfad: Wo sie liegt.
        ausdruecklich: Ob der Pfad ausdruecklich genannt wurde (Argument oder
            `MINERU_OCR_CONFIG`). Dann ist eine fehlende Datei ein Fehler; bei
            der Standarddatei ist sie der Normalfall.

    Raises:
        ConfigurationError: Datei fehlt trotz ausdruecklicher Nennung, ist
            unlesbar, kein gueltiges TOML, oder kennt einen Schluessel nicht. Der
            Pfad steht in jeder Meldung — er ist die erste Stelle, an der man
            nachsieht.
    """
    if not pfad.exists():
        if ausdruecklich:
            raise ConfigurationError(
                f"Die Konfigurationsdatei {pfad} existiert nicht, wurde aber "
                f"ausdruecklich genannt."
            )
        return {}
    try:
        inhalt = pfad.read_bytes()
    except OSError as fehler:
        raise ConfigurationError(f"{pfad} ist nicht lesbar: {fehler}") from fehler
    try:
        daten = tomllib.loads(inhalt.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as fehler:
        raise ConfigurationError(f"{pfad} ist kein gueltiges TOML: {fehler}") from fehler
    unbekannt = sorted(set(daten) - DATEI_SCHLUESSEL)
    if unbekannt:
        raise ConfigurationError(
            f"{pfad} kennt den Schluessel {unbekannt} nicht. Bekannt sind: "
            f"{sorted(DATEI_SCHLUESSEL)}. Der RunPod-Schluessel steht nicht hier, "
            f"sondern in {RUNPOD_CONFIG_PATH}."
        )
    return daten


def _wert(
    name: str, umgebung: Mapping[str, str], datei: Mapping[str, Any], pfad: Path
) -> tuple[Any, str] | None:
    """Der Wert eines Schluessels samt seiner Herkunft; `None`, wenn keiner.

    Zuerst die Umgebung, dann die Datei. Die Herkunft („MINERU_OCR_IDLE_TIMEOUT"
    oder „idle_timeout in <pfad>") gehoert in jede Fehlermeldung zum Wert: Wer
    einen falschen Wert hat, soll wissen, wo er ihn suchen muss.

    Eine leere oder nur aus Leerraum bestehende Angabe gilt als nicht gesetzt: Eine
    leere Umgebungsvariable ist fast immer ein Rest aus einem Skript und kein
    Wunsch, den Wert auf „nichts" zu setzen.
    """
    schluessel = ENV_PREFIX + name.upper()
    aus_umgebung = umgebung.get(schluessel)
    if aus_umgebung is not None and aus_umgebung.strip():
        return aus_umgebung, schluessel
    aus_datei = datei.get(name)
    if aus_datei is None or (isinstance(aus_datei, str) and not aus_datei.strip()):
        return None
    return aus_datei, f"{name} in {pfad}"


def _zeichenkette(name: str, wert: Any, herkunft: str) -> str:
    """Ein Text ohne Leerraum am Rand.

    Der Fehler nennt den Typ und nie den Wert: Eine Tabelle statt des DSN-Textes
    kann ein Passwort tragen, und die Meldung geht in die Ausgabe.
    """
    if not isinstance(wert, str):
        raise ConfigurationError(
            f"{herkunft}: `{name}` muss ein Text sein, ist aber vom Typ "
            f"{type(wert).__name__}."
        )
    return wert.strip()


def _zahl(name: str, wert: Any, herkunft: str) -> int:
    """Eine Ganzzahl aus Umgebung (Text) oder Datei (Zahl)."""
    if isinstance(wert, str):
        try:
            return int(wert)
        except ValueError:
            pass
    elif isinstance(wert, int) and not isinstance(wert, bool):
        return wert
    raise ConfigurationError(
        f"{herkunft}: `{name}` muss eine ganze Zahl sein, ist aber {wert!r}."
    )


def _namen(name: str, wert: Any, herkunft: str) -> tuple[str, ...]:
    """Eine Liste von Namen: in der Umgebung durch Kommas getrennt, in der Datei
    eine Liste. Leer ist ein Fehler — eine leere Liste liesse nichts zu."""
    if isinstance(wert, str) and herkunft.startswith(ENV_PREFIX):
        namen = [teil.strip() for teil in wert.split(",") if teil.strip()]
    elif isinstance(wert, list) and all(isinstance(n, str) for n in wert):
        namen = [n.strip() for n in wert if n.strip()]
    else:
        raise ConfigurationError(
            f"{herkunft}: `{name}` muss eine Liste von Namen sein, ist aber {wert!r}."
        )
    if not namen:
        raise ConfigurationError(f"{herkunft}: `{name}` ist leer.")
    return tuple(namen)


def _sollwerte(
    umgebung: Mapping[str, str], datei: Mapping[str, Any], pfad: Path
) -> dict[str, Any]:
    """Die Sollwerte des Endpoints, die irgendwo eingestellt sind.

    Nur was eingestellt ist, kommt in das Ergebnis; alles andere behaelt seine
    Feld-Voreinstellung. Die Endpoint-Variablen stehen nur in der Datei (als
    Tabelle `[endpoint_env]`) und ueberschreiben die Voreinstellung je
    Schluessel: Was die Tabelle nicht nennt, bleibt.
    """
    ergebnis: dict[str, Any] = {}
    for name in ZAHL_SCHLUESSEL:
        gefunden = _wert(name, umgebung, datei, pfad)
        if gefunden is not None:
            ergebnis[name] = _zahl(name, *gefunden)
    gefunden = _wert("expected_image_tag", umgebung, datei, pfad)
    if gefunden is not None:
        ergebnis["expected_image_tag"] = _zeichenkette("expected_image_tag", *gefunden)
    gefunden = _wert("allowed_data_centers", umgebung, datei, pfad)
    if gefunden is not None:
        ergebnis["allowed_data_centers"] = _namen("allowed_data_centers", *gefunden)

    tabelle = datei.get("endpoint_env")
    if tabelle is not None:
        herkunft = f"endpoint_env in {pfad}"
        if not isinstance(tabelle, dict):
            raise ConfigurationError(
                f"{herkunft}: muss eine Tabelle `[endpoint_env]` sein, ist aber {tabelle!r}."
            )
        env: dict[str, str | None] = dict(DEFAULT_ENDPOINT_ENV)
        for variable, soll in tabelle.items():
            if isinstance(soll, bool) or not isinstance(soll, (str, int)):
                raise ConfigurationError(
                    f"{herkunft}: Der Sollwert von `{variable}` muss ein Text oder "
                    f"eine Zahl sein, ist aber {soll!r}."
                )
            env[variable] = str(soll)
        ergebnis["endpoint_env"] = env
    return ergebnis


def _fehlt(name: str, config_path: Path) -> str:
    """Die Meldung fuer einen Pflichtwert, der nirgends eingestellt ist.

    Nennt jede Stelle, an der man ihn einstellen kann — „Konfigurationsfehler"
    allein hilft beim Suchen niemandem.
    """
    return (
        f"`{name}` ist nirgends eingestellt. Es gibt dafuer keine "
        f"Voreinstellung im Code. Einstellen: als Argument, als Umgebungsvariable "
        f"{ENV_PREFIX}{name.upper()} oder als `{name} = \"…\"` in {config_path}."
    )


# --------------------------------------------------------------------------
# Namen, die zwischen Prozessen verabredet sind
# --------------------------------------------------------------------------
# Diese Zeichenketten sind Vertrag zwischen Prozessen, die einander nie sehen:
# der Produzent reiht unter einem Task-Namen ein, der Daemon holt unter demselben
# ab. Stimmen sie nicht ueberein, passiert NICHTS — kein Fehler, keine Meldung,
# die Jobs bleiben schlicht liegen. Deshalb stehen sie hier und werden von
# beiden Seiten importiert, statt an zwei Stellen getippt zu werden
# (Feature-Verifikation Punkt 13: Producer- und Daemon-App muessen dieselben
# Task-Namen kennen).

TASK_OCR_DOCUMENT = "mineru_ocr.ocr_document"
"""Voller Name des Dokument-Tasks. Wird explizit gesetzt statt aus dem Modulpfad
abgeleitet, damit ein spaeteres Verschieben der Datei die laufende Warteschlange
nicht entwertet."""

TASK_RETRY_STALLED = "mineru_ocr.retry_stalled"
"""Voller Name des Waechter-Tasks (Plan 5.2)."""

QUEUE_OCR = "ocr"
"""Queue der Dokument-Jobs."""

QUEUE_MAINTENANCE = "maintenance"

DEFAULT_WAIT_POLL_SECONDS: Final = 5.0
"""Abstand der Rueckfallabfrage beim Warten auf ein Ergebnis, in Sekunden.

Als Konstante und nicht als Zahl im Feld: `Settings.wait_poll_seconds` benutzt
sie, und `db.wait_for_terminal` ebenfalls — und dessen Vorgabewert war vorher
eine zweite, fest verdrahtete `5.0`. Der Docstring am Feld verbietet genau das
(„weil eine Zahl an zwei Stellen beim naechsten Mal an einer geaendert wird"),
und die Verletzung stand direkt daneben."""

GUELTIGE_BACKENDS: Final = frozenset(
    {
        "hybrid-auto-engine",
        "hybrid-http-client",
        "pipeline",
        "vlm-auto-engine",
        "vlm-http-client",
    }
)
"""Die Backend-Namen, die der Worker annimmt.

VON DER GEGENSEITE ABGELESEN, nicht ausgedacht. Am 09.08.2026 wurde absichtlich
ein ungueltiger Name geschickt; die Fehlerantwort nannte die Liste selbst:

    „input validation failed: backend must be one of ['hybrid-auto-engine',
    'hybrid-http-client', 'pipeline', 'vlm-auto-engine', 'vlm-http-client'];
    got 'gibt-es-nicht'"

WOZU DIE PRUEFUNG HIER STEHT UND NICHT ERST DORT: Ein Tippfehler im Backend-
Namen war bis dahin nur zu erfahren, indem man dafuer bezahlte. Der Job wird
eingereiht, ein Worker faehrt hoch — im gemessenen Fall 177 Sekunden Wartezeit —,
und erst dann kommt `FAILED` mit der Meldung zurueck. Bei einem Bestandslauf
ueber 30 000 Dokumente traefe das JEDES Dokument einzeln, und es waere jedes Mal
derselbe Fehler.

Die Liste kann veralten, wenn der Worker neue Backends bekommt. Das ist der
bewusst in Kauf genommene Preis: Ein hier faelschlich abgelehnter Name kostet
eine Zeile in dieser Datei, ein nicht abgelehnter kostet einen Kaltstart je
Dokument. Der Nachpruef-Ort steht in der Fehlermeldung selbst — der Worker
nennt seine Liste, wenn man ihn falsch fragt."""
"""Queue des Waechters. Eigene Queue, weil periodische Tasks sonst um dieselben
Concurrency-Slots konkurrieren wie die OCR-Jobs und bei vollem Pool verhungern —
die Procrastinate-Doku empfiehlt fuer genau dieses Problem die Queue-Trennung
(Feature-Verifikation Punkt 2)."""

NOTIFY_CHANNEL = "doc_done"
"""Kanal, auf dem der Task jedes Terminal-Schreiben meldet; Nutzlast ist der
sha256 des Dokuments. EIGENBAU: Procrastinate hat kein eingebautes
„Job fertig"-Signal an externe Warter (Feature-Verifikation Punkt 10)."""


# --------------------------------------------------------------------------
# Die Einstellungen
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Vollstaendige Betriebseinstellung. Unveraenderlich, damit sie waehrend eines
    Laufs nicht unter der Hand kippt.

    Wird einmal beim Start gebildet (`Settings.load`) und dann durchgereicht.
    Es gibt bewusst KEINE globale Instanz und keinen Zugriff auf Umgebungsvariablen
    ausserhalb von `load` — sonst haengt das Verhalten eines Tests davon ab, was
    zufaellig in der Shell steht.
    """

    # -- Verbindungen ------------------------------------------------------

    db_dsn: str
    """Verbindung zur eigenen Datenbank. Kommt aus der Konfiguration (siehe
    `load`), nicht aus dem Code: wo die Datenbank liegt, ist Sache des Betriebs.
    Eigene Datenbank statt eigenem Schema in einer fremden, damit Sicherung,
    Loeschen und Migrationen sich nie mit den Daten anderer Anwendungen
    beruehren (Plan 9)."""

    endpoint_id: str
    """Der RunPod-Serverless-Endpoint. Kommt aus der Konfiguration (siehe
    `load`); es gibt keine Voreinstellung, denn jede im Code waere ein
    bestimmter Endpoint fuer jeden Aufrufer."""

    api_key: str
    """RunPod-API-Schluessel. Gehoert NIE in eine URL und NIE in eine Ausgabe;
    er reist ausschliesslich im Authorization-Header als Bearer-Token
    (Plan 8, Regel 12). `__repr__` dieser Klasse maskiert ihn (siehe unten)."""

    # -- Job-Contract ------------------------------------------------------

    backend: str = "vlm-auto-engine"
    """Das gemessene Backend. `hybrid-auto-engine` liess bei der Messung 13 von
    41 Formeln fallen und verlor stillschweigend Indizes, bei 2,2-facher Laufzeit.
    Der Wert ist ein Parameter und kein festverdrahteter Wert, weil die
    Vergleichsmessung fuer andere Materialarten aussteht — aber die Voreinstellung
    ist das Gemessene, nicht das Bequeme (Plan 11).

    Geprueft wird er gegen `GUELTIGE_BACKENDS`, siehe dort."""

    transport: str = "tarball_b64"
    """Fest. Bei diesem Transport ist ein `formats`-Feld wirkungslos: es kommt
    immer alles zurueck. Damit ist die Abruf-Regel („alles holen, alles
    persistieren, nie filtern") Mechanik statt Disziplin — an Disziplin ist sie
    schon einmal gescheitert. Zusaetzlich traegt der Tarball das Layout-PDF und
    die Bilder, die der Inline-Transport gar nicht liefert (Plan 6). Das
    Span-PDF gehoerte frueher in diese Aufzaehlung; es entsteht mit unserem
    Backend gar nicht erst (Begruendung an `assembly.ChunkArtifacts.span_pdf`)."""

    # -- Zerlegung grosser Dokumente --------------------------------------

    max_payload_bytes: int = 7_000_000
    """Obergrenze fuer die ROHE Groesse eines Chunks vor der Base64-Kodierung.
    Hergeleitet: Das Gateway nimmt auf `/run` hoechstens 10 MB an; Base64 blaeht
    um Faktor 1,33; der Rest ist Puffer fuer den Umschlag ringsum. 7,0 MB roh
    ergeben rund 9,3 MB kodiert."""

    max_pages_per_chunk: int = 20
    """Obergrenze fuer die Seitenzahl eines Chunks.

    AM TARBALL NACHGEMESSEN am 09.08.2026, an den fuenf damals konservierten
    echten Antworten unter `artifacts/2026-08-08-erster-tarball-lauf/` (vier
    davon sind vor dem ersten Push entfernt worden, weil sie personenbezogene
    Daten trugen, Mineroo#2; die Messung bleibt gueltig):

        41,5 KB je Seite — ein dichter Einzelseiter mit Tabelle und Bild
        29,5 KB je Seite — fuenf Seiten dichter Fliesstext mit Bildern
        16,2 KB je Seite — fuenf Seiten Deckblatt und Inhaltsverzeichnis
        10,1 KB je Seite — zwei Seiten Ablaufplan
         1,9 KB je Seite — eine leere Seite

    Der schlechteste gemessene Wert ist also rund 41,5 KB je Seite. Bei 20
    Seiten sind das etwa 0,81 MB Antwort gegen ein Cap von 20 MB — die Schwelle
    hat Faktor 25 Reserve, und das Cap waere erst bei rund 490 Seiten je
    Teilstueck ausgereizt.

    DIE SCHAETZUNG, DIE HIER STAND, LAG UM ZWEI GROESSENORDNUNGEN DANEBEN: Sie
    rechnete mit „konservativ 0,75 MB je Seite" und kam auf rund 15 MB fuer 20
    Seiten, also knapp unter das Cap. Gemessen sind es 0,81 MB. Der Fehler lag
    in der Zutat, nicht in der Rechnung — angenommen war, der Tarball trage
    zusaetzlich zu den Inline-Formaten noch Layout- UND Span-PDF; das Span-PDF
    gibt es gar nicht (siehe `assembly.ChunkArtifacts.span_pdf`), und das
    Layout-PDF ist mit rund 40 KB klein.

    Die Schwelle BLEIBT trotzdem bei 20, und das ist eine Entscheidung und kein
    Versaeumnis: Die Antwortgroesse ist nicht die einzige Grenze. Ein groesseres
    Teilstueck heisst auch laengere Verarbeitung je Job, mehr verlorene Arbeit
    bei einem Fehlschlag und ein groesseres Fenster gegen die
    Dreissig-Minuten-Aufbewahrung. Wer sie erhoehen will, hat mit dieser Messung
    jetzt den Kopfraum belegt — und sollte die anderen drei Groessen mitdenken."""

    # -- Zeitverhalten -----------------------------------------------------

    http_connect_timeout: float = 15.0
    """Wie lange auf das Zustandekommen einer Verbindung gewartet wird."""

    http_read_timeout: float = 60.0
    """Wie lange auf eine Antwort gewartet wird, nachdem die Anfrage draussen ist.
    Grosszuegig, weil `/status` gelegentlich traege antwortet und ein Abbruch hier
    einen laufenden, bezahlten Job zu einem Fehlversuch machen wuerde."""

    http_write_timeout: float = 300.0
    """Wie lange das HOCHLADEN dauern darf. Der wichtigste der vier Werte, und der
    Grund, warum es sie ueberhaupt einzeln gibt: httpx setzt ohne Zutun EINE
    Schranke von fuenf Sekunden fuer Verbindungsaufbau, Lesen, Schreiben und Pool
    zusammen (`httpx/_config.py`, `DEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0)`).
    Ein Teilstueck geht mit bis zu 9,3 MB Base64 hinaus — gegen fuenf Sekunden ist
    das bei maessigem Upstream nicht zu schaffen. Und der Fehlschlag waere teuer:
    Ein Schreib-Abbruch gilt als wiederholbar, der Chunk geht noch einmal raus,
    waehrend der erste Job auf der Gegenseite moeglicherweise schon angenommen und
    abgerechnet ist — und dessen Kennung kennt dann niemand. Fuenf Minuten sind
    grosszuegig gesetzt: Hochladen kostet nichts, ein doppelt bezahlter Parse
    schon."""

    http_pool_timeout: float = 30.0
    """Wie lange auf einen freien Platz im Verbindungspool gewartet wird."""

    http_max_keepalive: int = 32
    """Wie viele Verbindungen offen gehalten werden. Muss mindestens so gross sein
    wie `ocr_concurrency`, sonst baut ein Teil der gleichzeitig betreuten
    Dokumente bei JEDER Abfrage die TLS-Verbindung neu auf — httpx haelt ohne
    Zutun nur zwanzig offen (`httpx/_config.py`, `DEFAULT_LIMITS`), bei
    vierundzwanzig Dokumenten also zu wenige. Der Abstand nach oben ist Puffer
    fuer die GraphQL-Aufrufe."""

    poll_interval_seconds: float = 10.0
    """Abstand zwischen zwei `/status`-Abfragen eines laufenden Jobs.

    Der Sicherheitsabstand, um den es geht: Das Abholfenster der Gegenseite ist
    rund dreissig Minuten, also 1800 Sekunden. Bei zehn Sekunden Takt liegt die
    Verzoegerung zwischen „fertig" und „abgeholt" im Mittel bei fuenf Sekunden —
    das Hundertachtzigfache an Reserve. Enger zu takten kauft davon nichts
    Messbares und macht das Log unlesbar: Vierundzwanzig gleichzeitig betreute
    Dokumente ergeben bei zehn Sekunden 2,4 kleine Anfragen pro Sekunde, bei
    einer Sekunde waeren es vierundzwanzig.

    Es gibt auf dieser Schnittstelle nichts Besseres als Abfragen, und das ist
    geprueft, nicht angenommen: `/runsync` kann zwar bis zu fuenf Minuten warten,
    verkuerzt aber die Aufbewahrung von dreissig Minuten auf EINE und deckt den
    Kaltstart von achteinhalb Minuten ohnehin nicht. `/stream` ist keine offen
    gehaltene Verbindung — der Hersteller-SDK setzt es selbst als Schleife mit
    einer Sekunde Pause um, der Server antwortet also sofort. Ein Webhook waere
    echtes Push, braucht aber eine von aussen erreichbare Adresse. Bleibt
    `/status`, und dessen Antwort traegt im Moment des Fertigwerdens gleich das
    ganze Ergebnis — Abfrage und Abholung sind derselbe Aufruf."""

    job_timeout_seconds: float = 1_500.0
    """Zeitbudget fuer EIN Teilstueck: die Zeit, in der RunPod den Job bearbeitet
    (nicht `IN_QUEUE`), ueber alle Laeufe des Jobs zusammen. Warteschlange und
    Kaltstart zaehlen nicht mit: Sonst wuerde ein Job, der hinter einem grossen
    Lauf wartet, bei Ablauf abgebrochen (`/cancel`) und neu eingereiht, immer
    wieder und jedes Mal neu bezahlt. Dieselbe Frist gilt fuer eine Reihe
    fehlgeschlagener Abfragen (RunPod nicht erreichbar). 25 Minuten, damit ein
    Parse auch dann reicht, wenn der Worker waehrend des Parsens erst Modelle
    laedt. Wird das Budget gerissen, geht `/cancel` fuer den Job hinaus; der
    Fehler ist wiederholbar, nicht endgueltig."""

    ocr_concurrency: int = 24
    """Wie viele Dokumente der Daemon gleichzeitig betreut. GESETZT, nicht
    gemessen — nachzumessen am ersten echten Stapel (Plan 12.1).

    Die Zahl hat genau EINE sinnvolle Bezugsgroesse, und das ist wichtiger als
    ihr Wert: die tatsaechliche Parallelitaet auf der Gegenseite, naemlich
    `workersMax` mal `MINERU_MAX_CONCURRENCY`. Bei uns steht
    `MINERU_MAX_CONCURRENCY` auf 1 und die Worker-Quote bei 10 — es werden also
    zu keinem Zeitpunkt mehr als ZEHN Teilstuecke wirklich geparst. Mehr
    Dokumente gleichzeitig zu betreuen macht keinen einzigen Job schneller, es
    verlaengert nur die Warteschlange auf der Gegenseite.

    Warum trotzdem mehr als zehn: Ein Dokument traegt nur dann zur Warteschlange
    bei, wenn gerade ein Teilstueck von ihm draussen ist. Zwischen „Teilstueck
    fertig" und „naechstes abgeschickt" liegt bis zu ein Abfragetakt, und in
    dieser Luecke steuert es nichts bei. Bei rund dreissig Sekunden Parse-Zeit
    und zehn Sekunden Luecke ist etwa ein Viertel der Zeit tot; man braucht also
    gut dreizehn, um zehn Worker satt zu halten. Vierundzwanzig ist der Puffer
    darueber.

    DARAUS FOLGT DIE PFLEGEREGEL: Steigt die Worker-Quote, muss diese Zahl
    mitwachsen — sonst laufen die neuen Worker leer. Sinkt sie, ist ein zu hoher
    Wert nicht falsch, sondern nur wirkungslos (die Ueberzaehligen warten in der
    Warteschlange der Gegenseite). Wer diesen Wert anfasst, ohne die Quote zu
    kennen, raet."""

    maintenance_concurrency: int = 1
    """Der Waechter braucht genau einen Slot; mehr waere sinnlos, weil sein
    `queueing_lock` ohnehin nur einen wartenden Job zulaesst."""

    stalled_check_cron: str = "*/10 * * * *"
    """Wie oft der Waechter nach haengengebliebenen Jobs sieht. Der Wert stammt
    woertlich aus dem Doku-Muster von Procrastinate (Feature-Verifikation
    Punkt 6) — kein eigener Einfall."""

    shutdown_graceful_timeout: float = 30.0
    """Wie lange beim Beenden auf laufende Dokumente gewartet wird, bevor sie
    abgebrochen werden. Ein abgebrochener Job endet als `aborted`, und dafuer
    gibt es KEIN Retry-Bordmittel (Feature-Verifikation Punkt 19); sein
    Heilungsweg ist das erneute Einreihen. Deshalb ist der Wert eher grosszuegig:
    dreissig Sekunden reichen, damit ein laufender Chunk seine Antwort noch
    abholt."""

    # -- Geld --------------------------------------------------------------

    min_balance_start_usd: float = 1.00
    """Untergrenze im Preflight: unterhalb dessen beginnt eine Arbeit gar nicht
    erst. Ein Dollar ist Puffer, nicht Bedarf — Arbeit mit leerem Konto zu
    beginnen heisst, mitten im Bestand liegenzubleiben."""

    min_balance_pause_usd: float = 0.50
    """Untergrenze im Lauf: darunter geht der Task in die Warteschleife statt zu
    scheitern. Frueher als der historische Abbruch der Ad-hoc-Skripte bei
    fuenfzehn Cent, aus zwei Gruenden: Pausieren kostet nichts, und der Rest muss
    ausreichen, die bereits laufenden Chunks noch zu Ende zu bezahlen."""

    wait_poll_seconds: float = DEFAULT_WAIT_POLL_SECONDS
    """Abstand der Rueckfallabfrage in `wait()`. Das Wecksignal ist der Normalweg;
    dieser Wert deckt den Fall, dass es nicht ankommt (abgerissene Verbindung,
    Schreibvorgang von aussen). Steht hier und nicht als Zahl in zwei Docstrings,
    weil eine Zahl an zwei Stellen beim naechsten Mal an einer geaendert wird.

    Die Zahl selbst steht in `DEFAULT_WAIT_POLL_SECONDS` — genau wegen dieses
    Satzes: `db.wait_for_terminal` trug sie ein zweites Mal fest verdrahtet, also
    genau die Lage, die hier ausgeschlossen werden sollte."""

    balance_recheck_seconds: float = 900.0
    """Abstand der Kontostands-Pruefungen in der Warteschleife. Eine Viertelstunde
    ist keine Messung, sondern die Abwaegung zwischen „merkt das Aufladen schnell"
    und „belaestigt die API nicht ohne Not"."""

    # -- Sollwerte des Endpoints (gegen sie vergleicht der Preflight) ---------

    expected_image_tag: str | None = None
    """Der Tag, den das Template-Image und das Image jedes Workers tragen sollen.
    Der Betreiber stellt ihn nach jedem Build ein; vorher kennt ihn niemand,
    deshalb gibt es keine Voreinstellung — der Preflight meldet dann „nicht
    pruefbar", statt irgendeinen Tag fuer richtig zu halten."""

    workers_min: int = 0
    """`workersMin`: Skalierung auf null, damit ein ruhender Endpoint nichts kostet."""

    workers_max: int = 10
    """`workersMax`: die Kontogrenze. Steht es darunter, laeuft der Lauf langsamer,
    als er koennte; darueber akzeptiert RunPod den Wert nicht."""

    idle_timeout: int = 60
    """`idleTimeout` in Sekunden: wie lange ein Worker nach seinem letzten Job warm
    bleibt und bezahlt wird. Vorerst 60, wie am Endpoint gesetzt; den endgueltigen
    Wert setzt der Eigentuemer aus der gemessenen Kaltstartzeit."""

    execution_timeout_ms: int = 600000
    """`executionTimeoutMs`: wie lange ein Job laufen darf. Vorerst 600000, wie am
    Endpoint gesetzt; er folgt der Messung zur Teilstueck-Groesse."""

    allowed_data_centers: tuple[str, ...] = EWR_RECHENZENTREN
    """Die Rechenzentren, in denen der Endpoint rechnen darf. Der Preflight
    verlangt, dass ALLE Rechenzentren des Endpoints in dieser Liste liegen."""

    endpoint_env: Mapping[str, str | None] = dataclasses.field(
        default_factory=lambda: dict(DEFAULT_ENDPOINT_ENV)
    )
    """Endpoint-Variable → Sollwert, siehe `DEFAULT_ENDPOINT_ENV`."""

    # -- Betrieb -----------------------------------------------------------

    strict: bool = True
    """Ob eine Abweichung der Endpoint-Konfiguration den Preflight abbricht
    (`ConfigDriftError`) oder nur gemeldet wird."""

    delete_jobs: str = "never"
    """Job-Zeilen bleiben in der Datenbank stehen. Das ist zugleich Procrastinates
    Voreinstellung und Jurgens Festlegung („tidiness = full legibility"): Der
    abgeleitete Dokument-Status LIEST aus den Job-Zeilen; wer sie loescht, loescht
    die Beobachtbarkeit mit (Plan 5.3)."""

    max_attempts: int = 3
    """Wie oft ein Dokument bei wiederholbaren Fehlern insgesamt versucht wird.
    Achtung auf die Reichweite: Dieses Budget wird nicht nur von Strategie-Retries
    verbraucht, sondern von JEDER Wiedervorlage — auch von der stalled-Wiederaufnahme
    des Waechters und von einem Retry von Hand (Feature-Verifikation Punkt 19)."""

    retry_base_seconds: float = 30.0
    """Grundwartezeit des exponentiellen Rueckzugs."""

    retry_factor: float = 4.0
    """Faktor des Rueckzugs: rund 30 Sekunden, 2 Minuten, 8 Minuten."""

    def __post_init__(self) -> None:
        """Prueft die Werte, die die Gegenseite sonst erst gegen Bezahlung prueft.

        Hier steht AUSDRUECKLICH NUR, was ohne Netz und ohne Datenbank
        entscheidbar ist. Alles Weitere — laeuft der Endpoint, reicht das Konto,
        stimmt die Image-Fassung — gehoert in den Preflight, der fragen darf.

        Warum der Backend-Name dazugehoert: Er ist von hier aus vollstaendig
        pruefbar (die Gegenseite hat ihre Liste selbst genannt, siehe
        `GUELTIGE_BACKENDS`), und ein Tippfehler darin war bis dahin nur zu
        erfahren, indem man dafuer bezahlte — 177 Sekunden Kaltstart bis zur
        Fehlermeldung, und das je Dokument.

        Raises:
            ConfigurationError: Der Backend-Name steht nicht in
                `GUELTIGE_BACKENDS`.
        """
        if self.backend not in GUELTIGE_BACKENDS:
            raise ConfigurationError(
                f"backend={self.backend!r} kennt der Worker nicht. Gueltig sind: "
                f"{sorted(GUELTIGE_BACKENDS)}. Die Liste stammt aus der "
                f"Fehlerantwort des Workers selbst (09.08.2026) — sie kann "
                f"veralten, wenn er neue Backends bekommt; dann gehoert sie in "
                f"config.GUELTIGE_BACKENDS nachgetragen."
            )

    def __repr__(self) -> str:
        """Wie die Voreinstellung, aber mit maskiertem API-Schluessel.

        Diese Methode ist kein Komfort, sondern eine Sicherheitszusage: Eine
        Einstellung landet frueher oder spaeter in einem Log, einer Fehlermeldung
        oder einer Debugger-Ausgabe. `api_key` erscheint dort NIE im Klartext —
        stattdessen die Zeichenkette `***`. Wer den Schluessel wirklich braucht,
        liest ihn aus dem Feld, und das ist dann eine bewusste Handlung.

        WIE WEIT DIE ZUSAGE REICHT, damit sich niemand zu viel darauf verlaesst:
        Sie deckt `repr()` und darueber auch `str()`. Sie deckt NICHT
        `dataclasses.asdict(settings)` und nichts, was daraus JSON macht — dieser
        Weg geht an der Methode vorbei und schreibt den Schluessel im Klartext.
        Wer die Einstellung serialisiert, muss das Feld selbst entfernen. Es gibt
        im Paket keine Stelle, die das tut, und es soll auch keine geben.

        (Dass eine Dataclass diese Methode nicht ueberschreibt, ist geprueft:
        `dataclasses` setzt `__repr__` nur, wenn im Klassenkoerper keines steht.)
        """
        teile = []
        for feld in dataclasses.fields(self):
            wert = MASKE if feld.name == "api_key" else getattr(self, feld.name)
            teile.append(f"{feld.name}={wert!r}")
        return f"{type(self).__name__}({', '.join(teile)})"

    @classmethod
    def load(
        cls,
        *,
        db_dsn: str | None = None,
        endpoint_id: str | None = None,
        api_key: str | None = None,
        strict: bool = True,
        backend: str | None = None,
        min_balance_start_usd: float | None = None,
        ocr_concurrency: int | None = None,
        environ: Mapping[str, str] | None = None,
        config_path: Path | None = None,
    ) -> Settings:
        """Bildet die Einstellung aus Angabe, Umgebung, Datei und Voreinstellung.

        Rangfolge, vom Staerksten: ausdrueckliche Angabe → Umgebungsvariable
        `MINERU_OCR_<NAME>` → Konfigurationsdatei → Feld-Voreinstellung. Wer
        gerade aufruft, hat das letzte Wort; die Datei ist der Grundzustand.

        Die Datei ist TOML und liegt standardmaessig unter
        `~/.mineru_ocr/config.toml`; `MINERU_OCR_CONFIG` oder `config_path`
        zeigen auf eine andere. Fehlt die Standarddatei, ist das kein Fehler —
        eine ausdruecklich genannte, die fehlt, aber schon: Ein falsch getippter
        Pfad darf nicht still zur Voreinstellung werden.

        Endpoint und Datenbank haben KEINE Voreinstellung. Beide muessen
        irgendwo eingestellt sein, sonst wirft `load` mit einem Hinweis auf alle
        Stellen, an denen man das tut.

        Der API-Schluessel hat eine eigene Quelle: `~/.runpod/config.toml`, Feld
        `apikey` — der im Projekt bewaehrte Ort, an dem er ohnehin schon liegt.
        Er wird gelesen, nie geschrieben, und steht nie in der
        Konfigurationsdatei dieses Pakets.

        Args:
            db_dsn: Verbindungszeichenkette.
            endpoint_id: Kennung des RunPod-Endpoints.
            api_key: Voreinstellung siehe oben.
            strict: Ob Konfigurations-Abweichungen im Preflight abbrechen.
            backend: Voreinstellung `vlm-auto-engine`. Steht hier, weil
                `OCRClient` es als Konstruktor-Parameter anbietet — ein
                oeffentlicher Parameter, der keinen Weg in die Einstellung hat,
                ist ein stiller Nichts-Tuer: Wer `backend="pipeline"` schreibt,
                bekaeme keinen Fehler und trotzdem das alte Backend.
            min_balance_start_usd: Untergrenze im Preflight, aus demselben Grund.
            ocr_concurrency: Gleichzeitig betreute Dokumente, aus demselben Grund
                (`mineru-ocr daemon --concurrency`).
            environ: Die Umgebung, aus der gelesen wird. Voreinstellung
                `os.environ`; ein Test uebergibt eine eigene, damit sein Ergebnis
                nicht davon abhaengt, was zufaellig in der Shell steht.
            config_path: Die Konfigurationsdatei. Voreinstellung siehe oben.

        Returns:
            Die vollstaendige, unveraenderliche Einstellung.

        Zusicherung:
            Wirft, statt mit einer halben Einstellung weiterzulaufen. Legt NICHTS
            an; `load` ist frei von Seiteneffekten ausser dem Lesen der beiden
            Dateien.

        Raises:
            ConfigurationError: Kein API-Schluessel auffindbar; Endpoint oder
                Datenbank nirgends eingestellt; die Konfigurationsdatei ist
                unlesbar, kein gueltiges TOML oder kennt einen Schluessel nicht;
                eine ausdruecklich genannte Datei fehlt.

        Kosten: keine. Spricht weder mit RunPod noch mit der Datenbank —
        insbesondere prueft `load` NICHT, ob die Datenbank erreichbar oder der
        Schluessel gueltig ist. Das ist Sache des Preflights, und die Trennung ist
        Absicht: Eine Einstellung zu bilden muss auch offline gehen.
        """
        umgebung = os.environ if environ is None else environ
        # Eine leere `MINERU_OCR_CONFIG` nennt keine Datei (wie in `_wert`).
        aus_umgebung = umgebung.get(ENV_CONFIG_PATH, "").strip()
        ausdruecklich = config_path is not None or bool(aus_umgebung)
        if config_path is None:
            config_path = Path(aus_umgebung or DEFAULT_CONFIG_PATH)
        datei = _konfigurationsdatei(config_path, ausdruecklich=ausdruecklich)

        endpoint = endpoint_id
        if endpoint is None:
            gefunden = _wert("endpoint_id", umgebung, datei, config_path)
            if gefunden is None:
                raise ConfigurationError(_fehlt("endpoint_id", config_path))
            endpoint = _zeichenkette("endpoint_id", *gefunden)
        dsn = db_dsn
        if dsn is None:
            gefunden = _wert("db_dsn", umgebung, datei, config_path)
            if gefunden is None:
                raise ConfigurationError(_fehlt("db_dsn", config_path))
            dsn = _zeichenkette("db_dsn", *gefunden)

        einstellung = cls(
            db_dsn=dsn,
            endpoint_id=endpoint,
            api_key=api_key if api_key is not None else _schluessel_aus_datei(),
            strict=strict,
            **_sollwerte(umgebung, datei, config_path),
        )

        # Die drei uebrigen ueberschreiben ihre Feld-Voreinstellung nur, wenn sie
        # ausdruecklich angegeben wurden. Nacheinander mit `replace` statt in
        # einem Bruehwerk aus Schluesselwort-Argumenten: So bleibt jeder Schritt
        # getippt, und es braucht kein `type: ignore` — das eine, was in einer
        # Sammlung geprueft er Schnittstellen niemals stehen sollte, weil es
        # spaeter einen echten Fehler verdeckt.
        #
        # `is not None` und nicht Wahrheitswert-Pruefung: `0` und `0.0` sind
        # gueltige Angaben, und eine Null als „nicht gesetzt" zu lesen waere
        # genau die Art stiller Fehler, gegen die dieses Modul steht.
        if backend is not None:
            einstellung = dataclasses.replace(einstellung, backend=backend)
        if min_balance_start_usd is not None:
            einstellung = dataclasses.replace(
                einstellung, min_balance_start_usd=min_balance_start_usd
            )
        if ocr_concurrency is not None:
            einstellung = dataclasses.replace(
                einstellung, ocr_concurrency=ocr_concurrency
            )
        return einstellung
