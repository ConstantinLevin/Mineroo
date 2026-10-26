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
import math
import os
import re
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

TEILSTUECK_ZAHL_SCHLUESSEL: Final = (
    "max_pages_per_chunk",
    "max_payload_bytes",
)
"""Die Grenzen eines Teilstuecks, die Ganzzahlen sind (Mineroo#20). Das
Wartebudget je Teilstueck, `job_timeout_seconds`, ist eine Kommazahl."""

DATEI_SCHLUESSEL: Final = frozenset(
    {
        "endpoint_id",
        "db_dsn",
        "expected_image_tag",
        "allowed_data_centers",
        "endpoint_env",
        *ZAHL_SCHLUESSEL,
        *TEILSTUECK_ZAHL_SCHLUESSEL,
        "job_timeout_seconds",
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
    "MINERU_MAX_CONCURRENCY": "1",
    "MINERU_VIRTUAL_VRAM_SIZE": None,
}
"""Die Endpoint-Variablen, auf die es ankommt, mit ihrem Sollwert.

Geprueft wird an der Umgebung, die RunPod fuer die Worker meldet, und die
traegt nur Endpoint- und Template-Variablen und `RUNPOD_*`, keine des Images.
`MINERU_MODEL_SOURCE=local` (der Worker laedt seine Modelle offline aus dem
Image) setzt das Image des Forks selbst (`ENV` im `Dockerfile`); als Sollwert
hier waere es an jedem echten Worker eine Abweichung, die es nicht gibt (#31).
Wer es trotzdem am Endpoint setzt und geprueft haben will, nennt es in
`[endpoint_env]`. `MINERU_MAX_CONCURRENCY=1`: ein Teilstueck je Worker
zugleich, siehe `ocr_concurrency`. `MINERU_VIRTUAL_VRAM_SIZE` ist MinerUs Weg, den
GPU-Speicheranteil von vLLM einzustellen; ihr Wert kommt aus der Messung, die
noch aussteht. `None` heisst: Sollwert noch offen — der Preflight berichtet dann
den Ist-Wert und bewertet ihn als nicht pruefbar, statt ihn zu uebergehen."""

RUNPOD_CONFIG_PATH: Final = Path.home() / ".runpod" / "config.toml"
"""Der im Projekt bewaehrte Ort des API-Schluessels, Feld `apikey`. Wird
gelesen, nie geschrieben."""

MASKE: Final = "***"
"""Was anstelle des Schluessels in jeder Ausgabe steht."""

RUN_ANFRAGEGRENZE_BYTES: Final = 10_000_000
"""Die groesste JSON-Anfrage, die RunPod auf `/run` annimmt: 10 MB (RunPod-Doku,
„Payload limits"; Job-Vertrag des Forks, `file_b64`). Gegen sie prueft `Settings`
die Rohgroesse eines Teilstuecks (`max_payload_bytes`), denn ein zu grosses
scheitert erst am Gateway."""

UMSCHLAG_RESERVE_BYTES: Final = 100_000
"""Was neben dem Base64-Text in der Anfrage Platz braucht: der Umschlag
`{"input": {…}}` mit Stufe, Modus, Bildanalyse und Transport, gut 150 Byte.
Grosszuegig gerundet, weil ein Byte zu wenig den ganzen Job kostet."""

_GEHEIMNIS_TEILE: Final = frozenset(
    {
        "KEY", "KEYS", "APIKEY", "APIKEYS", "SECRET", "SECRETS", "SECRETKEY",
        "TOKEN", "PASSWORD", "PASSWD", "PASS", "PASSPHRASE", "PWD",
        "PASSWORDS", "ACCESSKEY", "PRIVATEKEY", "SSHKEY",
        "CREDENTIAL", "CREDENTIALS", "AUTH", "AUTHORIZATION", "BEARER",
    }
)
"""Die Namensteile, die eine Variable zum Geheimnis machen (grossgeschrieben)."""


def ist_geheimnisname(name: str) -> bool:
    """Ob der Wert einer Umgebungsvariable dieses Namens nie angezeigt wird.

    Der Name wird an allem zerlegt, was kein Buchstabe oder keine Ziffer ist
    (`HF_TOKEN`, `hf-token`), und ein GANZER Teil muss in `_GEHEIMNIS_TEILE`
    stehen: `HF_TOKEN`, `RUNPOD_AI_API_KEY`, `API_KEYS`, `DB_PASS` ja,
    `MAX_TOKENS`, `MONKEY` und `AUTHOR` nein. Ein Teilwort zaehlt nicht, sonst
    laese sich eine echte Abweichung nicht mehr; ein Teil wie `KEY` in
    `SORT_KEY` zaehlt dagegen, weil Verdecken der sichere Fehler ist. Die Liste
    bleibt unvollstaendig; sie gilt nur fuer Variablen, die der Betreiber selbst
    in `[endpoint_env]` nennt.
    """
    teile = re.split(r"[^A-Z0-9]+", name.upper())
    return any(teil in _GEHEIMNIS_TEILE for teil in teile)


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

def _schluessel_oder_nichts() -> str | None:
    """Der Schluessel aus `~/.runpod/config.toml`, oder `None`, wenn keiner
    nutzbar ist. Fuer `load`: Wer RunPod nicht anspricht, braucht ihn nicht, und
    die genaue Meldung kommt aus `Settings.require_api_key`."""
    try:
        return _schluessel_aus_datei()
    except ConfigurationError:
        return None


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


def _zeichenkette(name: str, wert: Any, herkunft: str, *, trimmen: bool = True) -> str:
    """Ein Text, standardmaessig ohne Leerraum am Rand.

    `trimmen=False` fuer einen DSN: libpq-Text, in dem ein maskiertes Leerzeichen
    am Ende zum Passwort gehoeren kann.

    Der Fehler nennt den Typ und nie den Wert: Eine Tabelle statt des DSN-Textes
    kann ein Passwort tragen, und die Meldung geht in die Ausgabe.
    """
    if not isinstance(wert, str):
        raise ConfigurationError(
            f"{herkunft}: `{name}` muss ein Text sein, ist aber vom Typ "
            f"{type(wert).__name__}."
        )
    return wert.strip() if trimmen else wert


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
        f"{herkunft}: `{name}` muss eine ganze Zahl sein, ist aber vom Typ "
        f"{type(wert).__name__}."
    )


def _sekunden(name: str, wert: Any, herkunft: str) -> float:
    """Eine Zahl, auch mit Komma, aus Umgebung (Text) oder Datei (Zahl). Ob sie
    endlich und groesser als null ist, prueft `Settings` selbst."""
    if isinstance(wert, str):
        try:
            return float(wert)
        except ValueError:
            pass
    elif isinstance(wert, (int, float)) and not isinstance(wert, bool):
        return float(wert)
    raise ConfigurationError(
        f"{herkunft}: `{name}` muss eine Zahl sein, ist aber vom Typ "
        f"{type(wert).__name__}."
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
            f"{herkunft}: `{name}` muss eine Liste von Namen sein, ist aber vom Typ "
            f"{type(wert).__name__}."
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
                f"{herkunft}: muss eine Tabelle `[endpoint_env]` sein, ist aber vom Typ "
                f"{type(tabelle).__name__}."
            )
        env: dict[str, str | None] = dict(DEFAULT_ENDPOINT_ENV)
        for variable, soll in tabelle.items():
            if isinstance(soll, bool) or not isinstance(soll, (str, int)):
                raise ConfigurationError(
                    f"{herkunft}: Der Sollwert von `{variable}` muss ein Text oder "
                    f"eine Zahl sein, ist aber vom Typ {type(soll).__name__}."
                )
            env[variable] = str(soll)
        ergebnis["endpoint_env"] = env
    return ergebnis


def _teilstueck_grenzen(
    umgebung: Mapping[str, str], datei: Mapping[str, Any], pfad: Path
) -> dict[str, Any]:
    """Die Grenzen eines Teilstuecks, die irgendwo eingestellt sind (Mineroo#20).

    Wie bei den Sollwerten kommt nur in das Ergebnis, was eingestellt ist; alles
    andere behaelt seine Feld-Voreinstellung. Ob ein Wert taugt (mindestens eine
    Seite, unter der Anfragegrenze von `/run`, ein endliches Budget), prueft
    `Settings` beim Bilden.
    """
    ergebnis: dict[str, Any] = {}
    for name in TEILSTUECK_ZAHL_SCHLUESSEL:
        gefunden = _wert(name, umgebung, datei, pfad)
        if gefunden is not None:
            ergebnis[name] = _zahl(name, *gefunden)
    gefunden = _wert("job_timeout_seconds", umgebung, datei, pfad)
    if gefunden is not None:
        ergebnis["job_timeout_seconds"] = _sekunden("job_timeout_seconds", *gefunden)
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

DAEMON_HEARTBEAT_MAX_AGE_SECONDS: Final = 30.0
"""Wie alt der juengste Herzschlag eines Workers hoechstens sein darf, damit der
Daemon als laufend gilt, in Sekunden.

Das ist Procrastinates eigene Grenze fuer „haengengeblieben": `stalled_worker_timeout`
steht in der Voreinstellung auf 30 s, und der Daemon aendert sie nicht. Ein Worker
meldet sich alle `update_heartbeat_interval` = 10 s; die Schwelle laesst also drei
Herzschlaege ausfallen, bevor der Daemon als tot gilt, und ein einzelner
verspaeteter macht ihn nicht tot. Die Zahl steht hier einmal, weil `status`,
`preflight` und das Abschicken dieselbe Frage stellen."""

GUELTIGE_STUFEN: Final = frozenset({"flash", "basic", "standard", "advanced"})
"""Die Stufen, die der MinerU-4-Worker annimmt: wie tief MinerU ein PDF oder Bild liest.

AUS DEM JOB-VERTRAG DES FORKS, nicht ausgedacht: `docs/content/docs/reference/api.mdx`
in `ConstantinLevin/mineru-runpod`, Abschnitt „Validation behaviour", Punkt 9
(„`tier` must be one of `flash`, `basic`, `standard`, `advanced`"). Sie loest die
Liste der Backends ab, die der Worker unter MinerU 3.4 nannte; `backend` weist er
jetzt ab und nennt, was an seine Stelle tritt.

WOZU DIE PRUEFUNG HIER STEHT UND NICHT ERST DORT: Ein Tippfehler waere sonst nur
zu erfahren, indem man dafuer bezahlt. Der Job wird eingereiht, ein Worker faehrt
hoch — im gemessenen Fall des alten Endpoints 177 Sekunden Wartezeit —, und erst
dann kommt `FAILED` mit der Meldung zurueck, bei einem Bestandslauf fuer jedes
Dokument einzeln. Die Liste kann veralten, wenn der Worker neue Stufen bekommt;
ein hier faelschlich abgelehnter Wert kostet eine Zeile in dieser Datei, ein nicht
abgelehnter einen Kaltstart je Dokument."""

GUELTIGE_OCR_MODI: Final = frozenset({"auto", "txt", "ocr"})
"""Die OCR-Modi, die der MinerU-4-Worker annimmt: woher der Text einer Seite kommt.

Aus derselben Stelle des Job-Vertrags („`ocr_mode` one of `auto`, `txt`, `ocr`").
`ocr` liest das Modell, `txt` nimmt die Textebene der PDF, `auto` laesst MinerU je
Datei waehlen. Geprueft aus demselben Grund wie `GUELTIGE_STUFEN`."""
"""Queue des Waechters. Eigene Queue, weil periodische Tasks sonst um dieselben
Concurrency-Slots konkurrieren wie die OCR-Jobs und bei vollem Pool verhungern —
die Procrastinate-Doku empfiehlt fuer genau dieses Problem die Queue-Trennung
(Feature-Verifikation Punkt 2)."""

NOTIFY_CHANNEL = "doc_done"
"""Kanal, auf dem der Task jedes Terminal-Schreiben meldet; Nutzlast ist der
sha256 des Dokuments. EIGENBAU: Procrastinate hat kein eingebautes
„Job fertig"-Signal an externe Warter (Feature-Verifikation Punkt 10)."""


# --------------------------------------------------------------------------
# Die Lese-Einstellungen eines Dokuments
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadSettings:
    """Wie MinerU 4 ein Dokument liest: Stufe, OCR-Modus und Bildanalyse.

    Die drei reisen immer zusammen (Befund S6d aus dem Review von Mineroo#11):
    Gewaehlt werden sie je Lauf beim Abschicken, abgelegt an der Eingabe, vom Job
    an RunPod geschickt und mit dem Ergebnis an der Zeile vermerkt (Mineroo#14).
    Ein Wert statt dreier Parameter, damit keine Stelle einen davon verliert.

    Die Voreinstellungen sind die des Job-Vertrags und dieses Pakets (Mineroo#1,
    Story 6 und 7): `advanced`, erzwungenes OCR, Bildanalyse an. `Settings`
    nimmt sie von hier; je Dokument kommen sie ueber `Settings.read_settings`.

    Geprueft wird beim Bilden, also vor dem Einreihen: Ein Tippfehler waere sonst
    nur gegen einen bezahlten Kaltstart je Dokument zu erfahren (siehe
    `GUELTIGE_STUFEN`).

    Die Bildanalyse wirkt laut Job-Vertrag nur in der Stufe `advanced`;
    ausserhalb davon ist sie hier als aus bestellt, gleich was angegeben ist. So
    sagt der Wert, was tatsaechlich an RunPod geht und was an der Zeile steht,
    und der Vergleich mit den Einstellungen, die der Worker meldet, geht auf.

    Raises:
        ConfigurationError: Stufe oder OCR-Modus steht nicht in seiner Liste, oder
            die Bildanalyse ist kein Wahrheitswert.
    """

    tier: str = "advanced"
    """Wie tief MinerU 4 liest (`GUELTIGE_STUFEN`)."""

    ocr_mode: str = "ocr"
    """Woher der Text kommt (`GUELTIGE_OCR_MODI`). `ocr` ist die Voreinstellung,
    weil die Textebene einer beschaedigten PDF der Schaden selbst ist: Im
    Textmodus liest das Modell Fliesstext, Titel, Kopf- und Fusszeilen,
    Seitenzahlen und Seitenfussnoten nicht selbst (Job-Vertrag, `ocr_mode`;
    Mineroo#1, Worker)."""

    image_analysis: bool = True
    """Ob das Modell Bilder und Diagramme beschreibt. Ausserhalb von `advanced`
    immer False (siehe oben)."""

    def __post_init__(self) -> None:
        for name, wert, gueltig in (
            ("tier", self.tier, GUELTIGE_STUFEN),
            ("ocr_mode", self.ocr_mode, GUELTIGE_OCR_MODI),
        ):
            # Erst der Typ: Eine Liste oder ein Woerterbuch liesse `in` mit einem
            # rohen `TypeError` scheitern statt mit der zugesicherten Meldung.
            if not isinstance(wert, str) or wert not in gueltig:
                raise ConfigurationError(
                    f"{name}={wert!r} kennt der Worker nicht. Gueltig sind: "
                    f"{sorted(gueltig)}. Die Liste stammt aus dem Job-Vertrag "
                    f"des Forks (api.mdx, „Validation behaviour“) — sie kann "
                    f"veralten, wenn der Worker neue Werte bekommt; dann gehoert "
                    f"sie in config nachgetragen."
                )
        if type(self.image_analysis) is not bool:
            # Ein Text wie "false" waere wahr und stuende so in der Eingabe.
            raise ConfigurationError(
                f"image_analysis={self.image_analysis!r} ist kein Wahrheitswert "
                f"(True oder False)."
            )
        if self.tier != "advanced" and self.image_analysis:
            # `object.__setattr__`, weil die Klasse eingefroren ist; das ist der
            # vorgesehene Weg in `__post_init__`, das noch zur Erzeugung gehoert.
            object.__setattr__(self, "image_analysis", False)


_LESEN_VOREINGESTELLT: Final = ReadSettings()
"""Die Voreinstellungen der Lese-Einstellungen, an EINER Stelle (`ReadSettings`);
`Settings` nimmt seine Feld-Voreinstellungen von hier."""


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

    endpoint_id: str | None
    """Der RunPod-Serverless-Endpoint. Kommt aus der Konfiguration (siehe
    `load`); es gibt keine Voreinstellung, denn jede im Code waere ein
    bestimmter Endpoint fuer jeden Aufrufer. `None` heisst „nirgends
    eingestellt": Das ist fuer `setup` und die lesenden Wege kein Fehler. Wer ihn
    braucht — Abschicken (die Zeile traegt ihn, bis sie erst am Ende des Jobs
    entsteht, #25), der Daemon, `preflight` —, holt ihn ueber
    `require_endpoint_id` und bekommt sonst die Meldung, wo man ihn einstellt."""

    api_key: str | None
    """RunPod-API-Schluessel. Gehoert NIE in eine URL und NIE in eine Ausgabe;
    er reist ausschliesslich im Authorization-Header als Bearer-Token
    (Plan 8, Regel 12). `__repr__` dieser Klasse maskiert ihn (siehe unten).
    `None` heisst „nicht gefunden": Abschicken, Lesen und `setup` brauchen nur
    die Datenbank. Wer RunPod anspricht, holt ihn ueber `require_api_key`."""

    # -- Job-Contract ------------------------------------------------------

    tier: str = _LESEN_VOREINGESTELLT.tier
    """Die Stufe, mit der MinerU 4 liest, wenn das Abschicken keine nennt.
    `advanced` ist die Voreinstellung des Job-Vertrags und dieses Pakets
    (Mineroo#1, Story 6). Geprueft gegen `GUELTIGE_STUFEN`.

    Die drei Lese-Einstellungen sind die VOREINSTELLUNG des Abschickens, nicht die
    des Daemons: Das Abschicken legt die eines Dokuments an seiner Eingabe ab
    (`read_settings`), und der Job schickt genau diese an RunPod (Mineroo#14). Was
    in der Einstellung des Daemons steht, wirkt auf keinen Job."""

    ocr_mode: str = _LESEN_VOREINGESTELLT.ocr_mode
    """Woher der Text kommt, wenn das Abschicken keinen Modus nennt
    (`ReadSettings.ocr_mode`). Geprueft gegen `GUELTIGE_OCR_MODI`."""

    image_analysis: bool = _LESEN_VOREINGESTELLT.image_analysis
    """Ob das Modell Bilder und Diagramme beschreibt, wenn das Abschicken nichts
    sagt. Wirkt nur in der Stufe `advanced`; ausserhalb davon ist sie als aus
    bestellt (`ReadSettings`)."""

    transport: str = "tarball_b64"
    """Fest. Bei diesem Transport ist ein `formats`-Feld wirkungslos: es kommt
    immer alles zurueck. Damit ist die Abruf-Regel („alles holen, alles
    persistieren, nie filtern") Mechanik statt Disziplin — an Disziplin ist sie
    schon einmal gescheitert. Der Tarball traegt jede Datei, die der Worker ins
    Ausgabeverzeichnis legt, auch eine, die das Paket noch nicht kennt
    (Job-Vertrag des Forks: „`formats` … No-op for `tarball_b64`")."""

    # -- Zerlegung grosser Dokumente --------------------------------------

    max_payload_bytes: int = 7_400_000
    """Obergrenze fuer die ROHE Groesse eines Teilstuecks vor der Base64-Kodierung.
    Ein eingestellter Wert (Schluessel `max_payload_bytes`, Mineroo#20).

    Hergeleitet aus der Anfragegrenze: `/run` nimmt hoechstens 10 MB JSON an
    (`RUN_ANFRAGEGRENZE_BYTES`), Base64 blaeht um 4/3. 7,4 MB roh ergeben
    9,87 MB kodiert, und es bleiben rund 130 KB fuer den Umschlag
    (`UMSCHLAG_RESERVE_BYTES`). Am Endpoint angenommen wurde am 27.10.2026 eine
    Anfrage mit 8,02 MB Base64 (6,02 MB roh). Ein groesserer Wert wirft beim
    Bilden der Einstellung, statt jedes grosse Teilstueck am Gateway scheitern zu
    lassen."""

    max_pages_per_chunk: int = 10
    """Obergrenze fuer die Seitenzahl eines Teilstuecks. Ein eingestellter Wert
    (Schluessel `max_pages_per_chunk`, Mineroo#20).

    Jede Grenze zwischen zwei Teilstuecken kann eine Tabelle zerreissen, weil
    MinerU Tabellen hoechstens innerhalb eines Jobs zusammenfuegt. Das Teilstueck
    ist deshalb so gross, wie die engste von drei Grenzen es traegt:

    ANTWORT (die engste). RunPod gibt auf `/run` hoechstens 10 MB Antwort zurueck
    (die Haelfte der 20 MB von `/runsync`). Belegt am 27.10.2026 mit einem Job
    aus 24 erfundenen dichten Scan-Seiten: Der Worker wies die Antwort mit
    `ResponseTooLargeError` ab (26,7 MB gegen seine eigene Obergrenze von
    19,4 MB) und nannte als Grund, dass das Gateway eine zu grosse Antwort
    verwirft und den Job trotzdem `COMPLETED` ohne Ausgabe meldet. Die
    dichtesten echten Scans des Messlaufs (Mineroo#19) brachten 729 KB je Seite.
    Mit 25 % Abstand zur Grenze, also 7,5 MB, tragen das 10 Seiten (7,3 MB).
    Die erfundenen Seiten des Belegs brachten 1,1 MB je Seite; Seiten dieser
    Dichte passten mit demselben Abstand nur zu sechst.

    ZEIT. Die langsamste gemessene Seite brauchte 5,0 s; mit dreifachem Puffer
    sind 10 Seiten 150 s, gut unter `executionTimeout` von 300 s.

    ANFRAGE. Die Bytes begrenzt `max_payload_bytes`; ein Teilstueck, das zu
    gross ist, wird halbiert (`chunking.iter_chunks`).

    Wer den Wert hebt, prueft alle drei: Ab 14 Seiten der dichtesten Scans
    reisst die Antwort die 10 MB. Reisst sie, endet das Dokument `failed` (der
    Worker weist ab, oder `COMPLETED` kommt ohne Ausgabe); der Weg zurueck ist
    ein kleinerer Wert und `retry`."""

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
    Ein Teilstueck geht mit bis zu 9,9 MB Base64 hinaus — gegen fuenf Sekunden ist
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

    job_timeout_seconds: float = 300.0
    """Zeitbudget fuer EIN Teilstueck: die Zeit, in der RunPod den Job bearbeitet
    (nicht `IN_QUEUE`), ueber alle Laeufe des Jobs zusammen. Ein eingestellter
    Wert (Schluessel `job_timeout_seconds`, Mineroo#20). Warteschlange und
    Kaltstart zaehlen nicht mit: Sonst wuerde ein Job, der hinter einem grossen
    Lauf wartet, bei Ablauf abgebrochen (`/cancel`) und neu eingereiht, immer
    wieder und jedes Mal neu bezahlt. Der Kaltstart samt Laden der Modelle
    (gemessen 104–128 s) liegt vor dem ersten Job eines Workers und damit in der
    Warteschlange. Dieselbe Frist gilt fuer eine Reihe fehlgeschlagener Abfragen
    (RunPod nicht erreichbar); das Ergebnis liegt dort danach noch bis zu
    dreissig Minuten. Wird das Budget gerissen, geht `/cancel` fuer den Job
    hinaus; der Fehler ist wiederholbar, nicht endgueltig.

    300 s aus derselben Rechnung wie `executionTimeout` des Endpoints
    (Mineroo#19): die langsamste gemessene Seite 5,0 s, mal 20 Seiten, mal drei.
    Bei 10 Seiten je Teilstueck (`max_pages_per_chunk`) ist das der doppelte
    Puffer; das laengste gemessene Teilstueck lief 17,8 s. Mehr als
    `executionTimeout` kauft nichts, denn dann beendet der Endpoint den Job
    schon selbst; weniger gaebe Teilstuecke auf, die der Endpoint noch laufen
    liesse. Wer Seiten je Teilstueck oder `executionTimeout` aendert, rechnet
    diesen Wert mit."""

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
    abgebrochen werden. Ein abgebrochener Job wird neu eingereiht, solange
    Versuche uebrig sind, und die Wiedervorlage zaehlt als Versuch; im letzten
    Versuch endet er `failed` (Mineroo#27). Das abgebrochene Dokument beginnt
    von vorn und bezahlt seine Teilstuecke neu. Deshalb ist der Wert eher
    grosszuegig: dreissig Sekunden reichen, damit ein laufender Chunk seine
    Antwort noch abholt."""

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
    bleibt und bezahlt wird. 60, wie am Endpoint gesetzt; den endgueltigen Wert
    setzt der Eigentuemer aus den gemessenen Kaltstartzeiten (Mineroo#19)."""

    execution_timeout_ms: int = 300000
    """`executionTimeoutMs`: wie lange ein Job laufen darf. 300000, wie am
    Endpoint gesetzt seit dem 26.10.2026: die langsamste gemessene Seite 5,0 s,
    mal 20 Seiten, mal drei (Mineroo#19). Das Wartebudget des Pakets je
    Teilstueck, `job_timeout_seconds`, folgt derselben Rechnung."""

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

    delete_jobs: str = "successful"
    """Erfolgreiche Jobs loescht der Worker, gescheiterte und abgebrochene bleiben
    stehen (Spec Mineroo#1, bisher `never`). An einem erfolgreichen Job haengt kein
    Zustand mehr: Sein Dokument hat ein Ergebnis, und die Eingabe samt ihrer
    Kennung `inputs.job_id` ist mit ihm gegangen. Die gescheiterten braucht
    `retry`: Es findet sie ueber `inputs.job_id` und setzt sie zurueck. Ein
    Aufraeumen ueber `remove_old_jobs` gibt es nicht; es bleiben wenige."""

    max_attempts: int = 3
    """Wie oft ein Dokument bei wiederholbaren Fehlern insgesamt versucht wird.
    Achtung auf die Reichweite: Dieses Budget wird nicht nur von Strategie-Retries
    verbraucht, sondern von JEDER Wiedervorlage — auch von der stalled-Wiederaufnahme
    des Waechters und von einem Retry von Hand (Feature-Verifikation Punkt 19)."""

    retry_base_seconds: float = 30.0
    """Grundwartezeit des exponentiellen Rueckzugs."""

    retry_factor: float = 4.0
    """Faktor des Rueckzugs: rund 30 Sekunden, 2 Minuten, 8 Minuten."""

    config_path: Path | None = dataclasses.field(default=None, compare=False)
    """Die Konfigurationsdatei, aus der `load` gelesen hat. Nur fuer Meldungen:
    `require_endpoint_id` nennt sie als eine der Stellen, an denen man den
    Endpoint einstellt."""

    def require_endpoint_id(self) -> str:
        """Der Endpoint, fuer die Wege, die ihn brauchen.

        Raises:
            ConfigurationError: Er ist nirgends eingestellt; die Meldung nennt
                jede Stelle, an der man ihn einstellt.
        """
        if self.endpoint_id is None:
            raise ConfigurationError(
                _fehlt("endpoint_id", self.config_path or DEFAULT_CONFIG_PATH)
            )
        return self.endpoint_id

    def require_api_key(self) -> str:
        """Der RunPod-Schluessel, fuer die Wege, die RunPod ansprechen.

        Fehlt er in der Einstellung, wird `~/.runpod/config.toml` jetzt gelesen:
        Die Meldung sagt dann genau, was mit der Datei ist (fehlt, kein TOML,
        kein Feld `apikey`), statt nur „kein Schluessel".

        Raises:
            ConfigurationError: Kein Schluessel uebergeben und keiner in der Datei.
        """
        if self.api_key is not None:
            return self.api_key
        return _schluessel_aus_datei()

    def __post_init__(self) -> None:
        """Prueft die Werte, die die Gegenseite sonst erst gegen Bezahlung prueft.

        Hier steht AUSDRUECKLICH NUR, was ohne Netz und ohne Datenbank
        entscheidbar ist. Alles Weitere — laeuft der Endpoint, reicht das Konto,
        stimmt die Image-Fassung — gehoert in den Preflight, der fragen darf.

        Warum Stufe und OCR-Modus dazugehoeren: Sie sind von hier aus
        vollstaendig pruefbar (der Job-Vertrag nennt die Listen, siehe
        `GUELTIGE_STUFEN` und `GUELTIGE_OCR_MODI`), und ein Tippfehler darin
        waere sonst nur zu erfahren, indem man dafuer bezahlt — ein Kaltstart bis
        zur Fehlermeldung, und das je Dokument.

        Geprueft wird ueber `ReadSettings`, die eine Stelle dafuer.

        Dazu die Grenzen eines Teilstuecks (Mineroo#20), seit sie eingestellte
        Werte sind: Null Seiten liessen `plan_chunks` nie fertig werden, ein
        Teilstueck ueber der Anfragegrenze von `/run` scheiterte erst am Gateway,
        und ein Budget von null oder `nan` gaebe jeden Job sofort auf, `inf` nie.

        Raises:
            ConfigurationError: Stufe oder OCR-Modus steht nicht in seiner Liste,
                oder eine Grenze eines Teilstuecks taugt nicht.
        """
        self.read_settings()
        if self.max_pages_per_chunk < 1:
            raise ConfigurationError(
                f"max_pages_per_chunk={self.max_pages_per_chunk!r}: Ein Teilstueck "
                f"braucht mindestens eine Seite."
            )
        kodiert = 4 * math.ceil(self.max_payload_bytes / 3) + UMSCHLAG_RESERVE_BYTES
        if self.max_payload_bytes < 1 or kodiert > RUN_ANFRAGEGRENZE_BYTES:
            raise ConfigurationError(
                f"max_payload_bytes={self.max_payload_bytes!r} taugt nicht: Ein "
                f"Teilstueck braucht mindestens ein Byte, und Base64 samt Umschlag "
                f"({kodiert} Byte) muss unter die Anfragegrenze von /run passen "
                f"({RUN_ANFRAGEGRENZE_BYTES} Byte)."
            )
        if not (math.isfinite(self.job_timeout_seconds) and self.job_timeout_seconds > 0):
            raise ConfigurationError(
                f"job_timeout_seconds={self.job_timeout_seconds!r}: Das Wartebudget "
                f"je Teilstueck muss eine endliche Zahl groesser als null sein."
            )

    def read_settings(
        self,
        *,
        tier: str | None = None,
        ocr_mode: str | None = None,
        image_analysis: bool | None = None,
    ) -> ReadSettings:
        """Die Lese-Einstellungen fuer ein Dokument: die angegebenen, sonst diese.

        Der Weg, auf dem ein Lauf seine eigenen waehlt (Mineroo#14): `ocr_start`,
        `ocr` und das Kommando `ocr` reichen ihre Angaben hierher, und was fehlt
        (None), kommt aus dieser Einstellung.

        Raises:
            ConfigurationError: Ein Wert ist ungueltig (`ReadSettings`).
        """
        return ReadSettings(
            tier=self.tier if tier is None else tier,
            ocr_mode=self.ocr_mode if ocr_mode is None else ocr_mode,
            image_analysis=self.image_analysis if image_analysis is None else image_analysis,
        )

    def __repr__(self) -> str:
        """Wie die Voreinstellung, aber mit maskiertem API-Schluessel.

        Diese Methode ist kein Komfort, sondern eine Sicherheitszusage: Eine
        Einstellung landet frueher oder spaeter in einem Log, einer Fehlermeldung
        oder einer Debugger-Ausgabe. `api_key` erscheint dort NIE im Klartext —
        stattdessen die Zeichenkette `***`. Wer den Schluessel wirklich braucht,
        liest ihn aus dem Feld, und das ist dann eine bewusste Handlung. Dasselbe
        gilt fuer die Sollwerte in `endpoint_env`, deren Name ein Geheimnis
        nennt (`ist_geheimnisname`, etwa `HF_TOKEN`).

        WIE WEIT DIE ZUSAGE REICHT, damit sich niemand zu viel darauf verlaesst:
        Sie deckt `repr()` und darueber auch `str()`. Sie deckt NICHT
        `dataclasses.asdict(settings)` und nichts, was daraus JSON macht — dieser
        Weg geht an der Methode vorbei und schreibt Schluessel und Sollwerte im
        Klartext. Wer die Einstellung serialisiert, muss diese Felder selbst
        entfernen. Es gibt
        im Paket keine Stelle, die das tut, und es soll auch keine geben.

        (Dass eine Dataclass diese Methode nicht ueberschreibt, ist geprueft:
        `dataclasses` setzt `__repr__` nur, wenn im Klassenkoerper keines steht.)
        """
        teile = []
        for feld in dataclasses.fields(self):
            wert: Any = getattr(self, feld.name)
            if feld.name == "api_key" and wert is not None:
                wert = MASKE
            if feld.name == "endpoint_env":
                wert = {
                    k: (MASKE if v is not None and ist_geheimnisname(k) else v)
                    for k, v in self.endpoint_env.items()
                }
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

        Endpoint und Datenbank haben KEINE Voreinstellung. Die Datenbank muss
        irgendwo eingestellt sein, sonst wirft `load` mit einem Hinweis auf alle
        Stellen, an denen man das tut. Der Endpoint darf fehlen (`None`): `setup`
        und die lesenden Wege brauchen ihn nicht, und wer ihn braucht, verlangt
        ihn mit `require_endpoint_id`.

        Der API-Schluessel hat eine eigene Quelle: `~/.runpod/config.toml`, Feld
        `apikey` — der im Projekt bewaehrte Ort, an dem er ohnehin schon liegt.
        Er wird gelesen, nie geschrieben, und steht nie in der
        Konfigurationsdatei dieses Pakets. Fehlt er (oder ist die Datei nicht
        nutzbar), ist das hier kein Fehler: Abschicken, Lesen und `setup`
        brauchen nur die Datenbank. Wer RunPod anspricht, verlangt ihn mit
        `require_api_key` und bekommt dort die genaue Meldung.

        Args:
            db_dsn: Verbindungszeichenkette.
            endpoint_id: Kennung des RunPod-Endpoints.
            api_key: Voreinstellung siehe oben.
            strict: Ob Konfigurations-Abweichungen im Preflight abbrechen.
            min_balance_start_usd: Untergrenze im Preflight. Steht hier, weil
                `OCRClient` sie als Konstruktor-Parameter anbietet — ein
                oeffentlicher Parameter, der keinen Weg in die Einstellung hat,
                ist ein stiller Nichts-Tuer.
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
            ConfigurationError: Die Datenbank ist nirgends eingestellt; die
                Konfigurationsdatei ist unlesbar, kein gueltiges TOML oder kennt
                einen Schluessel nicht; eine ausdruecklich genannte Datei fehlt.

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
            if gefunden is not None:
                endpoint = _zeichenkette("endpoint_id", *gefunden)
        dsn = db_dsn
        if dsn is None:
            gefunden = _wert("db_dsn", umgebung, datei, config_path)
            if gefunden is None:
                raise ConfigurationError(_fehlt("db_dsn", config_path))
            dsn = _zeichenkette("db_dsn", *gefunden, trimmen=False)

        einstellung = cls(
            db_dsn=dsn,
            endpoint_id=endpoint,
            api_key=api_key if api_key is not None else _schluessel_oder_nichts(),
            strict=strict,
            config_path=config_path,
            **_sollwerte(umgebung, datei, config_path),
            **_teilstueck_grenzen(umgebung, datei, config_path),
        )

        # Die zwei uebrigen ueberschreiben ihre Feld-Voreinstellung nur, wenn sie
        # ausdruecklich angegeben wurden. Nacheinander mit `replace` statt in
        # einem Bruehwerk aus Schluesselwort-Argumenten: So bleibt jeder Schritt
        # getippt, und es braucht kein `type: ignore` — das eine, was in einer
        # Sammlung geprueft er Schnittstellen niemals stehen sollte, weil es
        # spaeter einen echten Fehler verdeckt.
        #
        # `is not None` und nicht Wahrheitswert-Pruefung: `0` und `0.0` sind
        # gueltige Angaben, und eine Null als „nicht gesetzt" zu lesen waere
        # genau die Art stiller Fehler, gegen die dieses Modul steht.
        if min_balance_start_usd is not None:
            einstellung = dataclasses.replace(
                einstellung, min_balance_start_usd=min_balance_start_usd
            )
        if ocr_concurrency is not None:
            einstellung = dataclasses.replace(
                einstellung, ocr_concurrency=ocr_concurrency
            )
        return einstellung
