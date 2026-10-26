"""Einstellungen — und die einzige Zusage des Pakets, deren Bruch nicht rückholbar ist."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from mineru_ocr.config import (
    MASKE,
    Settings,
    ist_geheimnisname,
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


def test_ohne_endpoint_bildet_load_die_einstellung_und_wer_ihn_braucht_bekommt_den_hinweis() -> None:
    """Es gibt keine Voreinstellung fuer den Endpoint: Ein im Code eingetragener
    waere ein bestimmter Endpoint fuer jeden Aufrufer. `setup` und die lesenden
    Kommandos brauchen ihn nicht; wer ihn braucht (`require_endpoint_id`),
    bekommt eine Meldung, die beide Stellen nennt, an denen man ihn einstellt."""
    einstellung = Settings.load(api_key=GEHEIM, db_dsn=DSN, environ={})
    assert einstellung.endpoint_id is None
    with pytest.raises(ConfigurationError) as fehler:
        einstellung.require_endpoint_id()
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


def test_ohne_schluessel_bildet_load_die_einstellung_und_erst_runpod_verlangt_ihn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kein Schluessel uebergeben und keiner in `~/.runpod/config.toml`: Das ist
    fuer Abschicken, Lesen und `setup` kein Fehler, sie brauchen nur die
    Datenbank. Erst wer RunPod anspricht (`require_api_key`), bekommt die
    `ConfigurationError` — und kein leeres Feld."""
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", tmp_path / "fehlt.toml")
    einstellung = Settings.load()
    assert einstellung.api_key is None
    with pytest.raises(ConfigurationError, match="Kein API-Schluessel"):
        einstellung.require_api_key()


def test_schluessel_kommt_aus_der_datei(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ohne ausdrueckliche Angabe wird `apikey` aus der TOML-Datei gelesen."""
    datei = tmp_path / "config.toml"
    datei.write_text(f'apikey = "{GEHEIM}"\n', encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    assert Settings.load().api_key == GEHEIM
    assert Settings.load().require_api_key() == GEHEIM


def test_kaputte_datei_wirft_mit_pfad(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ungueltiges TOML ergibt `ConfigurationError`, und der Pfad steht in der
    Meldung — er ist die erste Stelle, an der man nachsieht."""
    datei = tmp_path / "config.toml"
    datei.write_text("das ist [[kein gueltiges toml\n", encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    einstellung = Settings.load()  # wer RunPod nicht anspricht, merkt nichts
    with pytest.raises(ConfigurationError, match=str(datei)):
        einstellung.require_api_key()


def test_datei_ohne_apikey_wirft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Gueltiges TOML ohne das Feld ist ebenso ein Konfigurationsfehler."""
    datei = tmp_path / "config.toml"
    datei.write_text('etwasanderes = "x"\n', encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    with pytest.raises(ConfigurationError, match="apikey"):
        Settings.load().require_api_key()


def test_leerer_apikey_wirft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ein leeres Feld ist kein Schluessel — sonst schluege erst der erste
    HTTP-Aufruf fehl, und zwar mit einer nichtssagenden Meldung."""
    datei = tmp_path / "config.toml"
    datei.write_text('apikey = "   "\n', encoding="utf-8")
    monkeypatch.setattr("mineru_ocr.config.RUNPOD_CONFIG_PATH", datei)
    einstellung = Settings.load()
    assert einstellung.api_key is None
    with pytest.raises(ConfigurationError):
        einstellung.require_api_key()


# --- Die Sollwerte des Endpoints ------------------------------------------


def test_sollwerte_haben_die_festgelegten_voreinstellungen() -> None:
    """Vorerst gelten die Werte, wie sie am Endpoint gesetzt sind (Mineroo#3):
    `workersMin` 0, `workersMax` 10, `idleTimeout` 60, `executionTimeoutMs`
    600000. Den Image-Tag kennt erst der Betreiber; ohne Angabe gibt es keinen."""
    einstellung = _einstellung()
    assert einstellung.workers_min == 0
    assert einstellung.workers_max == 10
    assert einstellung.idle_timeout == 60
    assert einstellung.execution_timeout_ms == 600000
    assert einstellung.expected_image_tag is None


def test_zulaessige_rechenzentren_sind_nur_ewr() -> None:
    """Die Voreinstellung der zulaessigen Rechenzentren liegt im EWR: EU-Staaten
    plus Island und Norwegen, denn dort gilt die DSGVO."""
    erlaubt = _einstellung().allowed_data_centers
    assert erlaubt, "eine leere Liste liesse nichts zu"
    assert all(
        name.startswith(("EU-", "EUR-IS-", "EUR-NO-")) for name in erlaubt
    ), erlaubt
    assert "EU-RO-1" in erlaubt
    assert "US-CA-2" not in erlaubt


def test_voreingestellte_endpoint_variablen() -> None:
    """Geprueft werden Modellquelle, Parallelitaet und GPU-Speicheranteil. Fuer
    den Speicheranteil gibt es noch keinen gemessenen Wert: Ohne Sollwert (`None`)
    steht er in der Liste, damit der Preflight ihn berichtet statt ihn zu
    vergessen."""
    env = _einstellung().endpoint_env
    assert env["MINERU_MODEL_SOURCE"] == "local"
    assert env["MINERU_MAX_CONCURRENCY"] == "1"
    assert env["MINERU_VIRTUAL_VRAM_SIZE"] is None


def test_sollwerte_kommen_aus_der_datei(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text(
        "expected_image_tag = \"abc123\"\n"
        "workers_min = 1\n"
        "workers_max = 4\n"
        "idle_timeout = 90\n"
        "execution_timeout_ms = 900000\n"
        "allowed_data_centers = [\"EU-NL-1\", \"EUR-NO-1\"]\n"
        "[endpoint_env]\n"
        "MINERU_MAX_CONCURRENCY = \"2\"\n"
        "MINERU_VIRTUAL_VRAM_SIZE = \"12\"\n"
        "NEUE_VARIABLE = \"x\"\n"
    )
    e = _einstellung(config_path=datei)
    assert e.expected_image_tag == "abc123"
    assert (e.workers_min, e.workers_max) == (1, 4)
    assert (e.idle_timeout, e.execution_timeout_ms) == (90, 900000)
    assert e.allowed_data_centers == ("EU-NL-1", "EUR-NO-1")
    # Die Tabelle ueberschreibt je Schluessel; was sie nicht nennt, bleibt.
    assert e.endpoint_env["MINERU_MAX_CONCURRENCY"] == "2"
    assert e.endpoint_env["MINERU_VIRTUAL_VRAM_SIZE"] == "12"
    assert e.endpoint_env["NEUE_VARIABLE"] == "x"
    assert e.endpoint_env["MINERU_MODEL_SOURCE"] == "local"


def test_sollwerte_kommen_aus_der_umgebung() -> None:
    e = _einstellung(
        environ={
            "MINERU_OCR_EXPECTED_IMAGE_TAG": "xyz",
            "MINERU_OCR_WORKERS_MAX": "3",
            "MINERU_OCR_IDLE_TIMEOUT": "120",
            "MINERU_OCR_ALLOWED_DATA_CENTERS": "EU-CZ-1, EU-SE-1",
        }
    )
    assert e.expected_image_tag == "xyz"
    assert e.workers_max == 3
    assert e.idle_timeout == 120
    assert e.allowed_data_centers == ("EU-CZ-1", "EU-SE-1")


def test_umgebung_schlaegt_datei_bei_den_sollwerten(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text("idle_timeout = 90\n")
    e = _einstellung(
        config_path=datei, environ={"MINERU_OCR_IDLE_TIMEOUT": "30"}
    )
    assert e.idle_timeout == 30


def test_zahl_in_der_umgebung_muss_eine_zahl_sein() -> None:
    with pytest.raises(ConfigurationError, match="MINERU_OCR_IDLE_TIMEOUT"):
        _einstellung(environ={"MINERU_OCR_IDLE_TIMEOUT": "sechzig"})


def test_zahl_in_der_datei_muss_eine_zahl_sein(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text('idle_timeout = "sechzig"\n')
    with pytest.raises(ConfigurationError, match="idle_timeout"):
        _einstellung(config_path=datei)


def test_rechenzentren_in_der_datei_sind_eine_liste_von_namen(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text('allowed_data_centers = "EU-RO-1"\n')
    with pytest.raises(ConfigurationError, match="allowed_data_centers"):
        _einstellung(config_path=datei)


def test_leere_umgebungsvariable_gilt_als_nicht_gesetzt(tmp_path: Path) -> None:
    """Ein Rest `MINERU_OCR_IDLE_TIMEOUT=` aus einem Skript soll die Datei nicht
    ueberstimmen — und auch nicht als Zahl scheitern."""
    datei = tmp_path / "config.toml"
    datei.write_text("idle_timeout = 90\n")
    e = _einstellung(config_path=datei, environ={"MINERU_OCR_IDLE_TIMEOUT": "  "})
    assert e.idle_timeout == 90


def test_wahrheitswert_ist_keine_zahl(tmp_path: Path) -> None:
    """`idle_timeout = true` ist in Python eine Ganzzahl und waere eine Sekunde."""
    datei = tmp_path / "config.toml"
    datei.write_text("idle_timeout = true\n")
    with pytest.raises(ConfigurationError, match="idle_timeout"):
        _einstellung(config_path=datei)


def test_leere_rechenzentrumsliste_wirft(tmp_path: Path) -> None:
    """Eine leere Liste liesse kein einziges Rechenzentrum zu."""
    datei = tmp_path / "config.toml"
    datei.write_text("allowed_data_centers = []\n")
    with pytest.raises(ConfigurationError, match="allowed_data_centers"):
        _einstellung(config_path=datei)
    with pytest.raises(ConfigurationError, match="MINERU_OCR_ALLOWED_DATA_CENTERS"):
        _einstellung(environ={"MINERU_OCR_ALLOWED_DATA_CENTERS": " , "})


def test_fehlende_datei_aus_der_umgebung_wirft(tmp_path: Path) -> None:
    """Auch ein falsch getippter `MINERU_OCR_CONFIG` darf nicht still zur
    Standarddatei werden."""
    fehlt = tmp_path / "fehlt.toml"
    umgebung = {
        "MINERU_OCR_CONFIG": str(fehlt),
        # Endpoint und Datenbank sind eingestellt: Allein die fehlende Datei darf
        # den Fehler ausloesen, nicht der fehlende Endpoint.
        "MINERU_OCR_ENDPOINT_ID": ENDPUNKT,
        "MINERU_OCR_DB_DSN": DSN,
    }
    with pytest.raises(ConfigurationError, match=str(fehlt)):
        Settings.load(api_key=GEHEIM, environ=umgebung)


def test_sollwerte_der_variablen_duerfen_zahlen_sein(tmp_path: Path) -> None:
    """`MINERU_MAX_CONCURRENCY = 2` ohne Anfuehrungszeichen ist gemeint wie
    `"2"`: Der Worker fuehrt Umgebungsvariablen immer als Text."""
    datei = tmp_path / "config.toml"
    datei.write_text("[endpoint_env]\nMINERU_MAX_CONCURRENCY = 2\n")
    assert _einstellung(config_path=datei).endpoint_env["MINERU_MAX_CONCURRENCY"] == "2"


def test_sollwert_einer_variable_darf_kein_wahrheitswert_sein(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text("[endpoint_env]\nMINERU_MAX_CONCURRENCY = true\n")
    with pytest.raises(ConfigurationError, match="MINERU_MAX_CONCURRENCY"):
        _einstellung(config_path=datei)


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


# --- Die nachgetragenen Parameter -----------------------------------------


def test_grenzen_kommen_an() -> None:
    """`min_balance_start_usd` und `ocr_concurrency` landen wirklich in der
    Einstellung.

    Der Test gegen den stillen Nichts-Tuer: Ein oeffentlicher Parameter ohne Weg
    in die Einstellung meldet keinen Fehler und wirkt trotzdem nicht.
    """
    einstellung = _einstellung(min_balance_start_usd=2.5, ocr_concurrency=8)
    assert einstellung.min_balance_start_usd == 2.5
    assert einstellung.ocr_concurrency == 8


def test_nicht_angegeben_heisst_voreinstellung() -> None:
    """Ohne Angabe gelten die Feld-Voreinstellungen."""
    einstellung = _einstellung()
    assert einstellung.tier == "advanced"
    assert einstellung.ocr_mode == "ocr"
    assert einstellung.image_analysis is True
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


def test_leere_konfigurationsvariable_gilt_als_nicht_gesetzt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ein leeres `MINERU_OCR_CONFIG` ist wie jede leere Variable ein Rest aus
    einem Skript: Es nennt keine Datei, also gilt die Standarddatei, und deren
    Fehlen ist kein Fehler."""
    monkeypatch.setattr("mineru_ocr.config.DEFAULT_CONFIG_PATH", tmp_path / "gibtes.nicht")
    for leer in ("", "   "):
        einstellung = Settings.load(
            api_key=GEHEIM,
            environ={
                "MINERU_OCR_CONFIG": leer,
                "MINERU_OCR_ENDPOINT_ID": ENDPUNKT,
                "MINERU_OCR_DB_DSN": DSN,
            },
        )
        assert einstellung.endpoint_id == ENDPUNKT


def test_text_einstellungen_werden_von_leerraum_befreit() -> None:
    """Ein Leerzeichen am Rand eines Tags oder einer Kennung ist nie gemeint; es
    liesse den Tag nie zu einem echten passen und stuende in der URL."""
    einstellung = _einstellung(
        environ={
            "MINERU_OCR_ENDPOINT_ID": "  testendpunkt\n",
            "MINERU_OCR_EXPECTED_IMAGE_TAG": " abc ",
        },
        endpoint_id=None,
    )
    assert einstellung.endpoint_id == "testendpunkt"
    assert einstellung.expected_image_tag == "abc"


def test_typfehler_bei_einem_text_nennt_den_wert_nicht(tmp_path: Path) -> None:
    """Eine Tabelle statt des DSN-Textes kann ein Passwort tragen. Die Meldung
    nennt Schluessel, Herkunft und Typ, nie den Wert."""
    datei = tmp_path / "config.toml"
    datei.write_text('[db_dsn]\nhost = "h"\npassword = "NICHT-AUSGEBEN"\n')
    with pytest.raises(ConfigurationError) as fehler:
        _einstellung(config_path=datei, db_dsn=None)
    meldung = str(fehler.value)
    assert "db_dsn" in meldung
    assert str(datei) in meldung
    assert "NICHT-AUSGEBEN" not in meldung


@pytest.mark.parametrize(
    "name",
    ["RUNPOD_AI_API_KEY", "HF_TOKEN", "DB_PASSWORD", "ADMIN_PASSWD", "SSH_PASSPHRASE",
     "DB_PWD", "CLIENT_SECRET", "AWS_CREDENTIALS", "AUTH", "BEARER", "PRIVATE_KEY",
     "APIKEY", "hf_token", "hf-token", "SECRETS", "API_KEYS", "DB_PASS",
     "HTTP_AUTHORIZATION", "PASSWORDS", "ACCESSKEY", "PRIVATEKEY", "SSHKEY"],
)
def test_geheimnisnamen_werden_erkannt(name: str) -> None:
    assert ist_geheimnisname(name)


@pytest.mark.parametrize(
    "name",
    ["MINERU_MODEL_SOURCE", "MINERU_MAX_CONCURRENCY", "MINERU_VIRTUAL_VRAM_SIZE",
     "MAX_TOKENS", "MAX_NEW_TOKENS", "AUTHOR", "KEYWORD", "MONKEY", "TURKEY",
     "PASSENGER", "COMPASS"],
)
def test_andere_namen_bleiben_sichtbar(name: str) -> None:
    """Ein Name, der nur ein Teilwort enthaelt, ist kein Geheimnis: Verdeckt
    waere eine echte Abweichung nicht mehr zu lesen."""
    assert not ist_geheimnisname(name)


def test_repr_zeigt_das_geheimnis_in_den_endpoint_variablen_nicht(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text('[endpoint_env]\nHF_TOKEN = "NICHT-AUSGEBEN"\nMINERU_MAX_CONCURRENCY = "2"\n')
    einstellung = _einstellung(config_path=datei)
    text = repr(einstellung)
    assert "NICHT-AUSGEBEN" not in text
    assert "HF_TOKEN" in text
    assert "'MINERU_MAX_CONCURRENCY': '2'" in text
    assert einstellung.endpoint_env["HF_TOKEN"] == "NICHT-AUSGEBEN"


@pytest.mark.parametrize(
    "inhalt",
    [
        'endpoint_env = "NICHT-AUSGEBEN"\n',
        '[endpoint_env]\nHF_TOKEN = ["NICHT-AUSGEBEN"]\n',
        '[endpoint_env]\nHF_TOKEN = 1.5\n',
        'workers_max = "NICHT-AUSGEBEN"\n',
        'allowed_data_centers = {a = "NICHT-AUSGEBEN"}\n',
    ],
)
def test_typfehler_der_sollwerte_nennen_den_wert_nicht(tmp_path: Path, inhalt: str) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text(inhalt)
    with pytest.raises(ConfigurationError) as fehler:
        _einstellung(config_path=datei)
    assert "NICHT-AUSGEBEN" not in str(fehler.value)


def test_der_dsn_bleibt_wie_geschrieben() -> None:
    """Ein DSN ist libpq-Text: Ein maskiertes Leerzeichen am Ende des Passworts
    (`password=a\\ `) ist Teil davon und darf nicht abgeschnitten werden."""
    dsn = "host=h password=geheim\\ "
    assert _einstellung(environ={"MINERU_OCR_DB_DSN": dsn}, db_dsn=None).db_dsn == dsn
