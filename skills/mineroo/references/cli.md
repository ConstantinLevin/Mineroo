# CLI verwenden

Für neues OCR gelten zuerst [Prüfung und Auftragsgrenze](ein-dokument.md).
`mineru-ocr PATH…` nimmt Dateien und Ordner, schickt ab, wartet und gibt Text aus.
Der [Stapelablauf](viele-dokumente.md) beschreibt mehrere Eingaben.

Für Audio gelten dieselben Aufrufe und Ausgabeoptionen, etwa
`mineru-ocr "aufnahme.m4a" -o transkript.md`. `--ra` enthält die komplette
ElevenLabs-Antwort samt gesendeten Parametern; `--re` lässt Audio unverändert.
USA-Verarbeitung und Credits: [Audio](technik.md#audio).

## Ausgabe wählen

| Bedarf | Aufruf nach der Prüfung |
| --- | --- |
| Vollständiges Markdown auf stdout | `mineru-ocr "dokument.pdf" --timeout 600` |
| Temporäre Textdatei; nur deren Pfad auf stdout | `mineru-ocr "dokument.pdf" -t --timeout 600` |
| Bestimmte UTF-8-Datei; nur deren Pfad auf stdout | `mineru-ocr "dokument.pdf" -o text.md --timeout 600` |
| HTML im Volltext, etwa Tabellen, als GFM-Markdown | `mineru-ocr "dokument.pdf" --re --timeout 600` |
| Vollständiges gespeichertes Ergebnis als JSON-Datei | `mineru-ocr "dokument.pdf" --ra -o ergebnis.json --timeout 600` |

`--re` lässt bestehendes Markdown außerhalb von HTML unverändert und lässt sich
mit `-t` oder `-o` kombinieren. `--ra` schreibt Dokumentangaben samt `raw` und
Bildern immer in eine Datei, ohne `-o` temporär; stdout nennt nur ihren Pfad.
Ein Stapel ergibt ein Array mit `path` je Eingabe. Auch gespeicherte Ergebnisse
mit `empty`, `failed` oder `input_error` stehen im JSON; die Exit-Codes bleiben.
`--ra` und `--re` schließen einander aus, ebenso `-t` und `-o`.

## Warten und Leseoptionen

Setze `--timeout S` als Wartefrist je Dokument. Nach Ablauf bleibt der Job
unterwegs; derselbe Pfad wartet später weiter. Ohne Angabe ist das Warten
unbegrenzt. Neue und offene Dokumente brauchen einen laufenden Daemon;
gespeicherte Ergebnisse kommen auch ohne ihn.

`-d` zeigt den bekannten Fortschritt je Datei auf stderr: Datenbanktreffer,
Warteschlange, laufenden Versuch, Wiederholungen und Zeiten. Lokale aktuelle
Daemons liefern auch Chunk-Plan, gesendete/beendete Chunks und RunPod-Zustände;
fehlende Angaben heißen `unbekannt`. Am Terminal aktualisiert sich die Anzeige;
umgeleitetes stderr enthält sachliche Änderungen ohne Steuerzeichen.
Bei Stapeln bewirkt `-d` außerdem das Einreihen vor dem Warten.
Bei Audio zeigt `-d` Warteschlange, beim lokalen aktuellen Daemon
`ElevenLabs verarbeitet` mit Schritt-/Gesamtzeit und den Endzustand. Chunk- und
RunPod-Felder entfallen, auch am schmalen Terminal. Ohne lokale Beobachtung
bleibt es beim bekannten Datenbankstand. Ohne `-d` entsteht keine Fortschrittsausgabe.

`--tier`, `--ocr-mode` und `--image-analysis`/`--no-image-analysis` wählen, wie
neue Dokumente gelesen werden. Vorgaben, Grenzen und die Entsprechungen in
Python: [Leseeinstellungen](technik.md#leseeinstellungen).

## Exit-Codes auswerten

| Code | Bedeutung |
| --- | --- |
| 0 | Erfolg, auch `empty` mit Grund ohne Text |
| 1 | Betriebs- oder Dateifehler |
| 2 | Aufruffehler |
| 3 | `failed` oder `input_error`, mit Grund je Datei |
| 4 | Wartefrist abgelaufen |
| 5 | Kein laufender Daemon; betroffene Datei nicht eingereiht |
| 130 | Strg-C |

Bei gemischten Ergebnissen gilt **1 vor 5 vor 4 vor 3 vor 0**. Erfolgreiche Texte
stehen trotzdem in der Ausgabe; prüfe zusätzlich die Meldungen auf stderr.

## Hilfe und Betriebskommandos

Die installierte Fassung und ihre Optionen stehen in `mineru-ocr --help`,
`mineru-ocr setup --help`, `mineru-ocr daemon --help` sowie
`mineru-ocr daemon start --help`, `mineru-ocr daemon stop --help` und
`mineru-ocr daemon list --help`. Jede Hilfeseite endet mit der Paketversion.
Einrichtung und Daemon-Verwaltung: [Technik](technik.md#einrichtung).
Zustände, Hash-Ziele und manuelles Wiederholen bedient die [Bibliothek](python.md);
dafür gibt es keine eigenen CLI-Kommandos.
