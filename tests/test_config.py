"""Einstellungen — und die einzige Zusage des Pakets, deren Bruch nicht rückholbar ist."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from mineru_ocr.config import (
    MASKE,
    Settings,
)
from mineru_ocr.errors import ConfigurationError

GEHEIM = "rpa_ERFUNDENER_SCHLUESSEL_NUR_FUER_TESTS_0123456789"
ENDPUNKT = "testendpunkt"
DSN = "host=testdb dbname=nur_im_test"


def _einstellung(**abweichend: object) -> Settings:
    """Eine gueltige Einstellung mit erfundenem Schluessel und leerer Umgebung.

    Endpoint und Datenbank stehen als ausdrueckliche Angabe darin, weil es fuer
    sie keine Voreinstellung mehr gibt; `environ={}` haelt die Shell des
    Entwicklers heraus.
    """
    angaben: dict[str, object] = {
        "api_key": GEHEIM,
        "endpoint_id": ENDPUNKT,
        "db_dsn": DSN,
        "environ": {},
    }
    angaben.update(abweichend)
    return Settings.load(**angaben)  # type: ignore[arg-type]


# --- Die Sicherheitszusage ------------------------------------------------


def test_repr_zeigt_den_schluessel_nicht() -> None:
    """`repr(settings)` enthaelt den Schluessel nicht und enthaelt `***`.

    Von allen Zusagen des Pakets ist das die eine, deren Bruch nicht rueckholbar
    ist: Ein Schluessel, der einmal in einem Log steht, steht dort.
    """
    text = repr(_einstellung())
    assert GEHEIM not in text
    assert MASKE in text


def test_str_zeigt_den_schluessel_nicht() -> None:
    """Auch ueber den Rueckfall `str()` kommt er nicht heraus."""
    assert GEHEIM not in str(_einstellung())


def test_repr_zeigt_die_uebrigen_felder_weiterhin() -> None:
    """Maskiert wird genau ein Feld, nicht die halbe Einstellung — sonst waere
    die Ausgabe zum Nachsehen unbrauchbar."""
    text = repr(_einstellung())
    assert ENDPUNKT in text
    assert "ocr_concurrency=24" in text


def test_asdict_zeigt_ihn_sehr_wohl() -> None:
    """`dataclasses.asdict` UMGEHT die Maskierung — der Test haelt die Grenze der
    Zusage fest, damit sich niemand zu viel darauf verlaesst.

    Er prueft damit ausdruecklich kein Wunschverhalten, sondern das tatsaechliche:
    Wer serialisiert, muss das Feld selbst entfernen.
    """
    assert dataclasses.asdict(_einstellung())["api_key"] == GEHEIM


def test_dataclass_ueberschreibt_repr_nicht() -> None:
    """Die Maskierung ueberlebt den Dataclass-Dekorator."""
    assert Settings.__repr__ is not object.__repr__
    assert "***" in repr(_einstellung())


# --- Quellen --------------------------------------------------------------


def test_uebergabe_schlaegt_voreinstellung() -> None:
    """Was uebergeben wurde, gilt; ohne Angabe gelten die Feld-Voreinstellungen,
    die keine Zugangsdaten und keine Betriebsmittel sind."""
    voreingestellt = _einstellung()

    eigen = _einstellung(db_dsn="host=anders", endpoint_id="xyz")
    assert eigen.db_dsn == "host=anders"
    assert eigen.endpoint_id == "xyz"


def test_endpoint_und_datenbank_kommen_aus_der_umgebung() -> None:
    """Ohne Angabe liest `load` beide aus Umgebungsvariablen — nicht aus dem Code."""
    einstellung = Settings.load(
        api_key=GEHEIM,
        environ={
            "MINERU_OCR_ENDPOINT_ID": "aus-der-umgebung",
            "MINERU_OCR_DB_DSN": "host=aus-der-umgebung",
        },
    )
    assert einstellung.endpoint_id == "aus-der-umgebung"
    assert einstellung.db_dsn == "host=aus-der-umgebung"


def test_endpoint_und_datenbank_kommen_aus_der_konfigurationsdatei(tmp_path: Path) -> None:
    """Die Konfigurationsdatei ist die zweite Quelle, wenn keine Umgebungsvariable
    gesetzt ist."""
    datei = tmp_path / "config.toml"
    datei.write_text('endpoint_id = "aus-der-datei"\ndb_dsn = "host=aus-der-datei"\n')
    einstellung = Settings.load(api_key=GEHEIM, environ={}, config_path=datei)
    assert einstellung.endpoint_id == "aus-der-datei"
    assert einstellung.db_dsn == "host=aus-der-datei"


def test_rangfolge_uebergabe_umgebung_datei(tmp_path: Path) -> None:
    """Angabe vor Umgebung vor Datei: je naeher an dem, der gerade aufruft, desto
    staerker."""
    datei = tmp_path / "config.toml"
    datei.write_text('endpoint_id = "datei"\ndb_dsn = "host=datei"\n')
    umgebung = {"MINERU_OCR_ENDPOINT_ID": "umgebung", "MINERU_OCR_DB_DSN": "host=umgebung"}

    aus_umgebung = Settings.load(api_key=GEHEIM, environ=umgebung, config_path=datei)
    assert aus_umgebung.endpoint_id == "umgebung"
    assert aus_umgebung.db_dsn == "host=umgebung"

    aus_angabe = Settings.load(
        api_key=GEHEIM,
        endpoint_id="angabe",
        db_dsn="host=angabe",
        environ=umgebung,
        config_path=datei,
    )
    assert aus_angabe.endpoint_id == "angabe"
    assert aus_angabe.db_dsn == "host=angabe"


def test_ohne_endpoint_wirft_load_mit_hinweis_auf_beide_quellen() -> None:
    """Es gibt keine Voreinstellung fuer den Endpoint: Ein im Code eingetragener
    waere ein bestimmter Endpoint fuer jeden Aufrufer. Die Meldung nennt beide
    Stellen, an denen man ihn einstellt."""
    with pytest.raises(ConfigurationError) as fehler:
        Settings.load(api_key=GEHEIM, db_dsn=DSN, environ={})
    text = str(fehler.value)
    assert "endpoint_id" in text
    assert "MINERU_OCR_ENDPOINT_ID" in text


def test_ohne_datenbank_wirft_load_mit_hinweis_auf_beide_quellen() -> None:
    with pytest.raises(ConfigurationError) as fehler:
        Settings.load(api_key=GEHEIM, endpoint_id=ENDPUNKT, environ={})
    text = str(fehler.value)
    assert "db_dsn" in text
    assert "MINERU_OCR_DB_DSN" in text


def test_fehlende_standarddatei_ist_kein_fehler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Wer alles ueber Umgebungsvariablen einstellt, braucht keine Datei."""
    monkeypatch.setattr("mineru_ocr.config.DEFAULT_CONFIG_PATH", tmp_path / "gibtes.nicht")
    einstellung = Settings.load(
        api_key=GEHEIM,
        environ={"MINERU_OCR_ENDPOINT_ID": "e", "MINERU_OCR_DB_DSN": "host=d"},
    )
    assert einstellung.endpoint_id == "e"


def test_standarddatei_wird_gelesen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ohne `config_path` und ohne `MINERU_OCR_CONFIG` gilt die Standarddatei."""
    datei = tmp_path / "standard.toml"
    datei.write_text('endpoint_id = "standard"\ndb_dsn = "host=standard"\n')
    monkeypatch.setattr("mineru_ocr.config.DEFAULT_CONFIG_PATH", datei)
    assert Settings.load(api_key=GEHEIM, environ={}).endpoint_id == "standard"


def test_pfad_der_konfigurationsdatei_kommt_aus_der_umgebung(tmp_path: Path) -> None:
    datei = tmp_path / "woanders.toml"
    datei.write_text('endpoint_id = "woanders"\ndb_dsn = "host=woanders"\n')
    einstellung = Settings.load(api_key=GEHEIM, environ={"MINERU_OCR_CONFIG": str(datei)})
    assert einstellung.endpoint_id == "woanders"


def test_ausdruecklich_genannte_datei_muss_es_geben(tmp_path: Path) -> None:
    """Ein falsch getippter Pfad darf nicht still zur Voreinstellung werden."""
    fehlt = tmp_path / "fehlt.toml"
    with pytest.raises(ConfigurationError, match=str(fehlt)):
        Settings.load(api_key=GEHEIM, environ={}, config_path=fehlt)


def test_kaputte_konfigurationsdatei_wirft_mit_pfad(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text("das ist [[kein gueltiges toml\n")
    with pytest.raises(ConfigurationError, match=str(datei)):
        Settings.load(api_key=GEHEIM, environ={}, config_path=datei)


def test_unbekannter_schluessel_in_der_datei_wirft(tmp_path: Path) -> None:
    """Ein Tippfehler im Schluesselnamen waere sonst ein stiller Nichts-Tuer: Der
    Wert stuende da und wirkte nirgends."""
    datei = tmp_path / "config.toml"
    datei.write_text('endpoint_id = "e"\ndb_dsn = "host=d"\nidle_timeuot = 5\n')
    with pytest.raises(ConfigurationError, match="idle_timeuot"):
        Settings.load(api_key=GEHEIM, environ={}, config_path=datei)


def test_ohne_schluessel_wirft_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Kein Schluessel uebergeben und keiner in `~/.runpod/config.toml`:
    `ConfigurationError`, nicht eine Einstellung mit leerem Feld."""
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", tmp_path / "fehlt.toml")
    with pytest.raises(ConfigurationError, match="Kein API-Schluessel"):
        Settings.load()


def test_schluessel_kommt_aus_der_datei(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ohne ausdrueckliche Angabe wird `apikey` aus der TOML-Datei gelesen."""
    datei = tmp_path / "config.toml"
    datei.write_text(f'apikey = "{GEHEIM}"\n', encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    assert Settings.load().api_key == GEHEIM


def test_kaputte_datei_wirft_mit_pfad(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ungueltiges TOML ergibt `ConfigurationError`, und der Pfad steht in der
    Meldung — er ist die erste Stelle, an der man nachsieht."""
    datei = tmp_path / "config.toml"
    datei.write_text("das ist [[kein gueltiges toml\n", encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    with pytest.raises(ConfigurationError, match=str(datei)):
        Settings.load()


def test_datei_ohne_apikey_wirft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Gueltiges TOML ohne das Feld ist ebenso ein Konfigurationsfehler."""
    datei = tmp_path / "config.toml"
    datei.write_text('etwasanderes = "x"\n', encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    with pytest.raises(ConfigurationError, match="apikey"):
        Settings.load()


def test_leerer_apikey_wirft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ein leeres Feld ist kein Schluessel — sonst schluege erst der erste
    HTTP-Aufruf fehl, und zwar mit einer nichtssagenden Meldung."""
    datei = tmp_path / "config.toml"
    datei.write_text('apikey = "   "\n', encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    with pytest.raises(ConfigurationError):
        Settings.load()


# --- Keine Seiteneffekte --------------------------------------------------


def test_einstellung_kennt_kein_spool_verzeichnis() -> None:
    """Die Eingabe liegt in `inputs`, nicht in einem Ordner: Es gibt keine
    Einstellung dafuer, und `load` nimmt keine an."""
    assert "spool_dir" not in {feld.name for feld in dataclasses.fields(Settings)}
    with pytest.raises(TypeError):
        Settings.load(api_key=GEHEIM, spool_dir="/tmp/irgendwo")  # type: ignore[call-arg]


def test_load_fragt_weder_datenbank_noch_endpoint() -> None:
    """Keine Verbindung, kein HTTP. Pruefen ist Sache des Preflights.

    Geprueft ueber eine offensichtlich unerreichbare Verbindungszeichenkette und
    eine erfundene Endpoint-Kennung: Wuerde `load` etwas davon anfassen, muesste
    es hier scheitern.
    """
    einstellung = _einstellung(
        db_dsn="host=192.0.2.1 port=1 dbname=gibtesnicht connect_timeout=1",
        endpoint_id="gibtesnicht",
    )
    assert einstellung.endpoint_id == "gibtesnicht"


# --- Die drei nachgetragenen Parameter ------------------------------------


def test_backend_und_grenzen_kommen_an() -> None:
    """`backend`, `min_balance_start_usd` und `ocr_concurrency` landen wirklich in
    der Einstellung.

    Der Test gegen den stillen Nichts-Tuer: Ein oeffentlicher Parameter ohne Weg
    in die Einstellung meldet keinen Fehler und wirkt trotzdem nicht.
    """
    einstellung = _einstellung(
        backend="pipeline", min_balance_start_usd=2.5, ocr_concurrency=8
    )
    assert einstellung.backend == "pipeline"
    assert einstellung.min_balance_start_usd == 2.5
    assert einstellung.ocr_concurrency == 8


def test_nicht_angegeben_heisst_voreinstellung() -> None:
    """Ohne Angabe gelten die Feld-Voreinstellungen."""
    einstellung = _einstellung()
    assert einstellung.backend == "vlm-auto-engine"
    assert einstellung.transport == "tarball_b64"
    assert einstellung.min_balance_start_usd == 1.0
    assert einstellung.ocr_concurrency == 24


def test_null_ist_eine_angabe() -> None:
    """`0` und `0.0` sind gueltige Werte und werden NICHT als „nicht gesetzt"
    gelesen.

    Der Unterschied zwischen `is not None` und einer Wahrheitswert-Pruefung —
    genau die Art stiller Fehler, gegen die dieses Modul steht.
    """
    einstellung = _einstellung(min_balance_start_usd=0.0, ocr_concurrency=0)
    assert einstellung.min_balance_start_usd == 0.0
    assert einstellung.ocr_concurrency == 0


# --- Unveraenderlichkeit --------------------------------------------------


def test_einstellung_ist_unveraenderlich() -> None:
    """Ein Schreibversuch an einem Feld wirft — sie darf waehrend eines Laufs
    nicht unter der Hand kippen."""
    einstellung = _einstellung()
    with pytest.raises(dataclasses.FrozenInstanceError):
        einstellung.ocr_concurrency = 99  # type: ignore[misc]


def test_http_zeitschranken_sind_gesetzt() -> None:
    """Alle vier HTTP-Zeitschranken haben einen Wert, und die Schreibgrenze ist
    deutlich groesser als httpx' Voreinstellung von fuenf Sekunden.

    Der Test gegen den Befund, an dem Geld haengt: Ein Upload von 9,3 MB gegen
    fuenf Sekunden reisst, der Chunk geht erneut raus, und der erste Job ist
    womoeglich schon abgerechnet.
    """
    einstellung = _einstellung()
    assert einstellung.http_connect_timeout > 0
    assert einstellung.http_read_timeout > 0
    assert einstellung.http_pool_timeout > 0
    assert einstellung.http_write_timeout >= 60.0


def test_keepalive_deckt_die_gleichzeitigkeit() -> None:
    """`http_max_keepalive` ist mindestens so gross wie `ocr_concurrency` —
    sonst baut ein Teil der Dokumente bei jeder Abfrage die Verbindung neu auf."""
    einstellung = _einstellung()
    assert einstellung.http_max_keepalive >= einstellung.ocr_concurrency
