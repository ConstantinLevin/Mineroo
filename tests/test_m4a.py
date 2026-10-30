"""M4A-Inhaltserkennung (#76), gueltige Medien ausschliesslich aus FFmpeg-lavfi."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest

from mineru_ocr import core, mime
from mineru_ocr.config import Settings


@pytest.fixture(scope="session")
def mp4_erzeugen(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., Path]:
    """Sinuston und Farbflaeche, keine Aufnahmen; ffprobe belegt die Spuren."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("M4A-Vertrag braucht ffmpeg und ffprobe fuer synthetische Medien.")
    wurzel = tmp_path_factory.mktemp("synthetische-mp4")

    def erzeugen(marke: str, spuren: str = "audio", schnellstart: bool = False, dauer: int = 1, vorspann: str = "", kapitel: bool = False) -> Path:
        ziel = wurzel / f"{marke.strip()}-{spuren}-{schnellstart}-{dauer}-{vorspann}-{kapitel}.m4a"
        if ziel.exists():
            return ziel
        befehl = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if "audio" in spuren:
            befehl += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
        if "video" in spuren:
            befehl += ["-f", "lavfi", "-i", "color=c=blue:s=32x32:r=5"]
        if kapitel:
            metadaten = wurzel / "kapitel.ffmetadata"
            metadaten.write_text(
                ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Erfundenes Kapitel\n",
                encoding="utf-8",
            )
            eingang = str(int("audio" in spuren) + int("video" in spuren))
            befehl += ["-f", "ffmetadata", "-i", str(metadaten), "-map_metadata", eingang, "-map_chapters", eingang]
        if spuren == "audio-video":
            befehl += ["-map", "0:a", "-map", "1:v"]
        befehl += ["-t", str(dauer), "-c:a", "aac", "-c:v", "mpeg4"]
        if schnellstart:
            befehl += ["-movflags", "+faststart"]
        befehl += ["-brand", marke, "-f", "mp4", str(ziel)]
        subprocess.run(befehl, check=True, capture_output=True, timeout=15)
        if vorspann:
            # Gueltige freie Box vor ftyp; Chunk-Offsets folgen den verschobenen
            # Samples. Die vollstaendige Dekodierung belegt die Gueltigkeit.
            daten = bytearray(ziel.read_bytes())
            stelle = 0
            while (stelle := daten.find(b"stco", stelle)) >= 0:
                anzahl = int.from_bytes(daten[stelle + 8:stelle + 12], "big")
                for nummer in range(anzahl):
                    offset = stelle + 12 + nummer * 4
                    wert = int.from_bytes(daten[offset:offset + 4], "big")
                    daten[offset:offset + 4] = (wert + 8).to_bytes(4, "big")
                stelle += 4
            ziel.write_bytes((8).to_bytes(4, "big") + vorspann.encode("ascii") + daten)
        if vorspann or kapitel:
            dekodiert = subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-i", str(ziel), "-f", "null", "-"],
                check=True, capture_output=True, timeout=15,
            )
            assert dekodiert.stderr == b""
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(ziel)],
            check=True, capture_output=True, timeout=10,
        )
        inhalt: dict[str, Any] = json.loads(probe.stdout)
        erwartet = {"audio": ["audio"], "video": ["video"], "audio-video": ["audio", "video"]}[spuren]
        if kapitel:
            erwartet = sorted([*erwartet, "data"])
            assert [s["codec_tag_string"] for s in inhalt["streams"] if s["codec_type"] == "data"] == ["text"]
        assert sorted(s["codec_type"] for s in inhalt["streams"]) == erwartet
        assert inhalt["format"]["tags"]["major_brand"] == marke
        if "audio" in spuren:
            assert [s["codec_name"] for s in inhalt["streams"] if s["codec_type"] == "audio"] == ["aac"]
        return ziel

    return erzeugen


@pytest.mark.parametrize("marke", ["mp42", "isom", "M4A ", "M4B "])
@pytest.mark.parametrize("schnellstart", [False, True])
@pytest.mark.parametrize("kapitel", [False, True])
def test_m4a_audio_unabhaengig_von_marke_und_name(
    mp4_erzeugen: Callable[..., Path], tmp_path: Path, settings: Settings,
    marke: str, schnellstart: bool, kapitel: bool,
) -> None:
    daten = mp4_erzeugen(marke, schnellstart=schnellstart, kapitel=kapitel).read_bytes()
    falsch_benannt = tmp_path / "inhalt.pdf"
    falsch_benannt.write_bytes(daten)
    assert mime.sniff_bytes(daten) is mime.DocumentType.M4A
    with falsch_benannt.open("rb") as datei:
        assert mime.sniff_file(datei) is mime.DocumentType.M4A
    assert core.prepare(daten, settings).doc_type is mime.DocumentType.M4A


@pytest.mark.parametrize("marke", ["mp42", "isom", "M4A ", "M4B "])
@pytest.mark.parametrize("spuren", ["video", "audio-video"])
@pytest.mark.parametrize("schnellstart", [False, True])
@pytest.mark.parametrize("kapitel", [False, True])
def test_keine_marke_laesst_video_durch(
    mp4_erzeugen: Callable[..., Path], tmp_path: Path, settings: Settings,
    marke: str, spuren: str, schnellstart: bool, kapitel: bool,
) -> None:
    from mineru_ocr.errors import UnsupportedTypeError

    daten = mp4_erzeugen(marke, spuren, schnellstart, kapitel=kapitel).read_bytes()
    with pytest.raises(UnsupportedTypeError):
        mime.sniff_bytes(daten)
    for name in ("video.m4a", "video.pdf"):
        pfad = tmp_path / name
        pfad.write_bytes(daten)
        with pfad.open("rb") as datei, pytest.raises(UnsupportedTypeError):
            mime.sniff_file(datei)
    with pytest.raises(UnsupportedTypeError):
        core.prepare(daten, settings)


@pytest.mark.db
@pytest.mark.parametrize("marke", ["mp42", "isom", "M4A ", "M4B "])
@pytest.mark.parametrize("kapitel", [False, True])
async def test_m4a_abschicken_und_retry_waehlen_audio(
    mp4_erzeugen: Callable[..., Path], db_conn: Any, settings: Settings, marke: str, kapitel: bool,
) -> None:
    from mineru_ocr.client import OCRClient, AsyncOCRClient
    from mineru_ocr.core import RetryOutcome

    daten = mp4_erzeugen(marke, dauer=10, kapitel=kapitel).read_bytes()
    assert daten.index(b"moov") > mime.KOPF_BYTES
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(daten)
        assert sha.queued
        assert not klient.ocr_start(daten).queued
    assert db_conn.execute("SELECT queue_name, task_name FROM procrastinate_jobs").fetchall() == [
        ("audio", "mineru_ocr.audio_document"),
    ]
    db_conn.execute("DELETE FROM procrastinate_jobs")
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        assert klient.retry(sha) is RetryOutcome.REQUEUED
        assert klient.retry(sha) is RetryOutcome.UNDERWAY
    assert db_conn.execute("SELECT queue_name, task_name FROM procrastinate_jobs").fetchall() == [
        ("audio", "mineru_ocr.audio_document"),
    ]
    db_conn.execute("DELETE FROM procrastinate_jobs")
    db_conn.execute("DELETE FROM inputs")
    async with AsyncOCRClient(db_dsn=settings.db_dsn) as klient_async:
        assert (await klient_async.ocr_start(daten)).queued
    assert db_conn.execute("SELECT queue_name, task_name FROM procrastinate_jobs").fetchall() == [
        ("audio", "mineru_ocr.audio_document"),
    ]


@pytest.mark.db
@pytest.mark.parametrize("marke", ["mp42", "isom", "M4A ", "M4B "])
@pytest.mark.parametrize("spuren", ["video", "audio-video"])
@pytest.mark.parametrize("kapitel", [False, True])
async def test_video_wird_ohne_eingabe_oder_job_abgewiesen(
    mp4_erzeugen: Callable[..., Path], db_conn: Any, settings: Settings,
    marke: str, spuren: str, kapitel: bool,
) -> None:
    from mineru_ocr.client import OCRClient, AsyncOCRClient
    from mineru_ocr.errors import UnsupportedTypeError

    daten = mp4_erzeugen(marke, spuren, kapitel=kapitel).read_bytes()
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        with pytest.raises(UnsupportedTypeError):
            klient.ocr_start(daten)
    async with AsyncOCRClient(db_dsn=settings.db_dsn) as klient_async:
        with pytest.raises(UnsupportedTypeError):
            await klient_async.ocr_start(daten)
    assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (0,)


@pytest.mark.parametrize("schaden", ["abgeschnitten", "kurzer-kopf", "kleine-box", "ueberlauf", "ohne-spur", "kurzer-handler", "nur-text", "unbekannter-handler", "ohne-handler"])
def test_beschaedigtes_m4a_wird_ohne_markenbonus_abgewiesen(
    mp4_erzeugen: Callable[..., Path], schaden: str,
) -> None:
    from mineru_ocr.errors import UnsupportedTypeError

    daten = mp4_erzeugen("M4A ").read_bytes()
    if schaden == "abgeschnitten":
        daten = daten[:-1]
    elif schaden == "kurzer-kopf":
        daten += b"1234"
    elif schaden == "kleine-box":
        daten = b"\x00\x00\x00\x04" + daten[4:]
    elif schaden == "ueberlauf":
        daten = b"\xff\xff\xff\xff" + daten[4:]
    elif schaden == "ohne-spur":
        daten = daten.replace(b"trak", b"free")
    elif schaden == "kurzer-handler":
        stelle = daten.index(b"hdlr") - 4
        daten = daten[:stelle] + (16).to_bytes(4, "big") + daten[stelle + 4:]
    elif schaden == "nur-text":
        daten = daten.replace(b"soun", b"text")
    elif schaden == "unbekannter-handler":
        daten = daten.replace(b"soun", b"frem")
    elif schaden == "ohne-handler":
        daten = daten.replace(b"hdlr", b"free")
    with pytest.raises(UnsupportedTypeError):
        mime.sniff_bytes(daten)
    with pytest.raises(UnsupportedTypeError):
        mime.sniff_file(io.BytesIO(daten))


def test_grosse_mediendaten_werden_bei_dateierkennung_uebersprungen(
    mp4_erzeugen: Callable[..., Path],
) -> None:
    class GezaehlteDatei(io.BytesIO):
        gelesen = 0

        def read(self, size: int | None = -1) -> bytes:
            daten = super().read(size)
            self.gelesen += len(daten)
            return daten

    daten = mp4_erzeugen("isom", dauer=10).read_bytes()
    datei = GezaehlteDatei(daten)
    assert mime.sniff_file(datei) is mime.DocumentType.M4A
    assert datei.gelesen < mime.KOPF_BYTES + 4096 < len(daten)


@pytest.mark.parametrize("variante", ["64-bit", "bis-dateiende"])
def test_mp4_boxgroessen_werden_gelesen(
    mp4_erzeugen: Callable[..., Path], tmp_path: Path, variante: str,
) -> None:
    daten = mp4_erzeugen("isom").read_bytes()
    if variante == "64-bit":
        # Die freie Acht-Byte-Box vor mdat wird dessen erweiterter Kopf;
        # Sample-Offets und Mediendaten bleiben dadurch an derselben Stelle.
        stelle = daten.index(b"mdat") - 4
        assert daten[stelle - 8:stelle] == b"\x00\x00\x00\x08free"
        groesse = int.from_bytes(daten[stelle:stelle + 4], "big")
        daten = daten[:stelle - 8] + b"\x00\x00\x00\x01mdat" + (groesse + 8).to_bytes(8, "big") + daten[stelle + 8:]
    else:
        stelle = daten.index(b"moov") - 4
        daten = daten[:stelle] + bytes(4) + daten[stelle + 4:]
    pfad = tmp_path / "boxgroesse.m4a"
    pfad.write_bytes(daten)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(pfad)],
        check=True, capture_output=True, timeout=10,
    )
    assert json.loads(probe.stdout)["streams"] == [{"codec_name": "aac", "codec_type": "audio"}]
    assert mime.sniff_bytes(daten) is mime.DocumentType.M4A
    with pfad.open("rb") as datei:
        assert mime.sniff_file(datei) is mime.DocumentType.M4A


@pytest.mark.db
@pytest.mark.parametrize("marke", ["mp42", "isom"])
@pytest.mark.parametrize("vorspann", ["free", "skip"])
def test_m4a_mit_freier_box_vor_ftyp_bleibt_audio_auch_beim_retry(
    mp4_erzeugen: Callable[..., Path], tmp_path: Path, db_conn: Any,
    settings: Settings, marke: str, vorspann: str,
) -> None:
    from mineru_ocr.client import OCRClient
    from mineru_ocr.core import RetryOutcome
    from mineru_ocr.errors import UnsupportedTypeError

    daten = mp4_erzeugen(marke, dauer=10, vorspann=vorspann).read_bytes()
    assert mime.sniff_bytes(daten) is mime.DocumentType.M4A
    pfad = tmp_path / "ton.pdf"
    pfad.write_bytes(daten)
    with pfad.open("rb") as datei:
        assert mime.sniff_file(datei) is mime.DocumentType.M4A
    with OCRClient(db_dsn=settings.db_dsn, preflight=False) as klient:
        sha = klient.ocr_start(daten)
        assert sha.queued
        assert db_conn.execute("SELECT queue_name, task_name FROM procrastinate_jobs").fetchall() == [
            ("audio", "mineru_ocr.audio_document"),
        ]
        db_conn.execute("DELETE FROM procrastinate_jobs")
        assert klient.retry(sha) is RetryOutcome.REQUEUED
        assert db_conn.execute("SELECT queue_name, task_name FROM procrastinate_jobs").fetchall() == [
            ("audio", "mineru_ocr.audio_document"),
        ]
        video = mp4_erzeugen(marke, "audio-video", vorspann=vorspann).read_bytes()
        with pytest.raises(UnsupportedTypeError):
            klient.ocr_start(video)
        assert db_conn.execute("SELECT count(*) FROM inputs").fetchone() == (1,)
        assert db_conn.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (1,)
    ohne_dateityp = daten.replace(b"ftyp", b"free", 1)
    with pytest.raises(UnsupportedTypeError):
        mime.sniff_bytes(ohne_dateityp)
