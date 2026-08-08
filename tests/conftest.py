"""Gemeinsame Vorrichtungen fuer alle Tests.

Zur Lage der Tests in Phase A: Jeder Test hier hat einen NAMEN und eine
Beschreibung, aber noch keinen Koerper. Das ist Absicht — ein Testname ist die
ausfuehrbare Fassung einer Zusicherung aus dem Vertrag. Solange die Koerper
fehlen, ist die Liste eine Aufgabenliste; sobald sie da sind, ist sie der Beweis.
Uebersprungene Tests werden von pytest gezaehlt und angezeigt, es kann also
keiner vergessen werden.

Was hier NICHT steht: eine Vorrichtung, die still eine Datenbank anlegt oder
einen Endpoint anspricht. Tests mit `db`-Kennzeichnung brauchen die laufende
Postgres-Instanz und sagen es; Tests mit `live`-Kennzeichnung kosten Geld und
laufen nur, wenn man sie ausdruecklich anfordert.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mineru_ocr.config import Settings

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "2026-08-05-doku-verifikationstest"
"""Die sechs konservierten ECHTEN Antworten vom Doku-Verifikationstest.

Sie sind das einzige echte Antwortmaterial, das es ohne einen bezahlten Lauf
gibt. Eine Einschraenkung, die man kennen muss: Sie stammen aus Laeufen mit
`transport: "inline"`, waehrend wir `tarball_b64` versenden. Sie tragen also den
Antwort-Umschlag, `markdown`, `content_list` mit `page_idx` und `middle` — aber
KEIN `tarball_b64` und keine Bilder. Fuer den Tarball-Pfad gibt es bis zum ersten
Live-Lauf kein Offline-Material; diese Luecke ist benannt und nicht ueberdeckt.

Eine zweite Einschraenkung, die genauso wichtig ist: ALLE SECHS SIND EINSEITIGE
BILDER (`blackscholes__-01.png` bis `-12.png`), ihre `page_idx` sind durchweg 0.
Damit gibt es auch fuer die Mehr-Teilstueck-Lage kein echtes Material — weder
fuer den Seitenversatz beim Zusammensetzen noch fuer die Bild-Vereinigung ueber
Teilstuecke hinweg. Beides laeuft gegen erfundene Antworten, bis der Live-Smoke
eine echte mehrteilige Antwort erzeugt hat. Ohne diesen Hinweis schliesst man aus
„die einzigen echten Antworten", die uebrigen Tests seien gegen echtes Material
abgesichert.
"""

TEST_DSN = "host=localhost port=5437 dbname=mineru_ocr_test user=jurgen"
"""Eigene Datenbank auf der laufenden Instanz. NICHT `mineru_ocr` und schon gar
nicht `gbrain` — ein Test, der die Arbeitsdatenbank leert, tut das genau einmal
und dann nie wieder, weil danach niemand mehr Tests laufen laesst."""


@pytest.fixture
def real_responses() -> list[dict[str, Any]]:
    """Die sechs konservierten Antworten, vollstaendig geladen."""
    return [
        json.loads(p.read_text())
        for p in sorted(FIXTURE_DIR.glob("*.response.json"))
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Eine Einstellung mit Testwerten und ohne echten Schluessel.

    Der Schluessel ist ausdruecklich erfunden und als solcher lesbar. Das ist
    keine Kosmetik: Faende ein Test doch den Weg ins Netz, soll die Ablehnung
    von der Gegenseite kommen und nicht ein bezahlter Lauf.

    Das Spool-Verzeichnis liegt unter dem Temporaerpfad des jeweiligen Tests und
    ist damit je Test ein anderes — die echte Voreinstellung ist
    `~/.mineru_ocr/spool/`, und ein Test, der dort hineinschreibt, hinterlaesst
    Dateien im Arbeitsverzeichnis des Benutzers.

    Die uebrigen Werte bleiben die Voreinstellungen aus `Settings`, damit die
    Tests gegen das laufen, was im Betrieb wirklich gilt. Wo ein Test eine
    andere Schwelle braucht — kleinere Chunks, kleinere Payload —, baut er sie
    sich mit `dataclasses.replace` aus dieser Vorrichtung, statt hier einen
    zweiten Satz Werte einzufuehren, der mit dem Betrieb auseinanderlaeuft.
    """
    return Settings(
        db_dsn=TEST_DSN,
        endpoint_id="testendpoint",
        api_key="erfundener-schluessel-nicht-echt",
        spool_dir=tmp_path / "spool",
    )


@pytest.fixture
def db_conn() -> Any:
    """Eine Verbindung zur Test-Datenbank, mit frischem Schema je Test.

    Wird in Phase B gefuellt. Legt die Datenbank an, wendet das Schema an, gibt
    die Verbindung heraus und raeumt danach auf.
    """
    pytest.skip("Vorrichtung folgt in Phase B; braucht die Instanz auf Port 5437")


@pytest.fixture
def mock_runpod() -> Any:
    """Ein RunPod-Zugang mit vorgegebenen Antworten, ohne Netz und ohne Kosten.

    Wird in Phase B gefuellt: ein `httpx.MockTransport`, der Absenden und
    Abfragen bedient und dabei die konservierten Antworten ausliefert. Der
    Transport ist die Naht, an der das Netz endet — `RunPodClient` nimmt einen
    fertigen HTTP-Client entgegen, genau dafuer.
    """
    pytest.skip("Vorrichtung folgt in Phase B")
