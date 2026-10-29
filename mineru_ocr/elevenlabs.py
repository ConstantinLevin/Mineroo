"""Scribe-v2-Anfrage und Transkript-Darstellung nach den Messungen in #69.

Eine synchrone HTTP-Antwort je Datei, ohne Webhook oder Polling. Der asynchrone
HTTP-Client haelt dabei die Ereignisschleife des Daemons frei.
"""
from __future__ import annotations

from typing import Any, Final

import httpx

from .config import Settings
from .errors import ConfigurationError, IncompleteResultError, WorkerRejectedError
from .mime import DocumentType

STT_URL: Final = "https://api.elevenlabs.io/v1/speech-to-text"
PARAMETER: Final = {
    "model_id": "scribe_v2", "diarize": "true", "tag_audio_events": "true",
}


async def transcribe(
    data: bytes, doc_type: DocumentType, *, settings: Settings, http: httpx.AsyncClient,
) -> dict[str, Any]:
    """Sendet Originalbytes und behaelt die gesamte JSON-Antwort samt Parametern.

    Keine automatische HTTP-Wiederholung: Jeder bezahlte Versuch gehoert der
    bestehenden Job-Strategie. Fehlertexte enthalten nie Anfragekopfzeilen.
    """
    schluessel = settings.require_elevenlabs_api_key()
    try:
        antwort = await http.post(
            STT_URL, headers={"xi-api-key": schluessel}, data=PARAMETER,
            files={"file": ("audio" + doc_type.suffix, data, doc_type.mime)},
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
    except httpx.TransportError as fehler:
        # Keine fremde Fehlermeldung uebernehmen: Sie koennte den Header tragen.
        raise httpx.TransportError("ElevenLabs: Transportfehler bei der Transkription.") from None
    if antwort.status_code in (401, 403):
        raise ConfigurationError("ElevenLabs: Schluessel ungueltig oder Zugriff verweigert.")
    if antwort.status_code == 422:
        raise WorkerRejectedError("ElevenLabs: Audiodatei nicht lesbar (HTTP 422).")
    if not antwort.is_success:
        # Die zentrale Einordnung und Kontopause werden in #71 ergaenzt.
        raise httpx.HTTPStatusError(
            f"ElevenLabs: HTTP {antwort.status_code} bei der Transkription.",
            request=httpx.Request("POST", STT_URL), response=antwort,
        )
    try:
        roh = antwort.json()
    except ValueError:
        raise IncompleteResultError("ElevenLabs: Antwort ist kein JSON.") from None
    if not isinstance(roh, dict):
        raise IncompleteResultError("ElevenLabs: Antwort ist kein JSON-Objekt.")
    return {"provider": "elevenlabs", "parameters": dict(PARAMETER), "response": roh}


def transcript(raw: dict[str, Any]) -> str:
    """Fliesstext mit einem Absatz je Sprecherwechsel; Ereignisse bleiben.

    Sprecher werden ab 1 in Reihenfolge ihres ersten Auftretens nummeriert.
    Ereignisse ohne Sprecher gehoeren zum laufenden Absatz. Leerraum aus den
    Wortdaten bleibt innerhalb eines Absatzes erhalten, an seinen Raendern nicht.
    """
    antwort = raw.get("response")
    if not isinstance(antwort, dict) or not isinstance(antwort.get("words"), list):
        raise IncompleteResultError("ElevenLabs: words fehlt oder ist keine Liste.")
    woerter = antwort["words"]
    nummern: dict[str | None, int] = {}
    absaetze: list[str] = []
    teile: list[str] = []
    sprecher: str | None = None
    begonnen = False
    hat_wort = False

    def abschliessen() -> None:
        text = "".join(teile).strip()
        if text:
            absaetze.append(f"Sprecher {nummern[sprecher]}: {text}")
        teile.clear()

    for wort in woerter:
        if not isinstance(wort, dict) or not isinstance(wort.get("text"), str):
            raise IncompleteResultError("ElevenLabs: Worteintrag ohne Text.")
        art = wort.get("type")
        if art not in ("word", "spacing", "audio_event"):
            raise IncompleteResultError("ElevenLabs: unbekannte Art eines Worteintrags.")
        if art == "spacing":
            teile.append(wort["text"])
            continue
        kennung = wort.get("speaker_id")
        if kennung is not None and not isinstance(kennung, str):
            raise IncompleteResultError("ElevenLabs: ungueltige Sprecherkennung.")
        if art == "audio_event" and kennung is None and not begonnen:
            teile.append(wort["text"])
            continue
        if art == "audio_event" and kennung is None and begonnen:
            kennung = sprecher
        if not begonnen or kennung != sprecher:
            if begonnen:
                abschliessen()
            sprecher = kennung
            nummern.setdefault(sprecher, len(nummern) + 1)
            begonnen = True
        teile.append(wort["text"])
        hat_wort |= art == "word" and bool(wort["text"].strip())
    if begonnen:
        abschliessen()
    return "\n\n".join(absaetze) if hat_wort else ""
