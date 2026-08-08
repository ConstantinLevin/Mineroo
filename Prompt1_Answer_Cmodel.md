Soll der lokale Orchestrator überhaupt existieren
Kurzurteil
Für euren konkreten Fall ist die nüchterne Antwort: ein großer lokaler Orchestrator ist sehr wahrscheinlich nicht mehr nötig, wenn ihr von RunPod Load-Balancing auf RunPod Queue-Based umstellt. Die Plattform übernimmt dort genau den Teil, der euch heute weh tut: Jobs werden in eine Queue gestellt statt wie im LB-Modus nach kurzer Wartezeit verworfen, Scale-from-zero ist nativ, und die Job-Lifecycle-API ist für Batch-Verarbeitung gebaut (/run, /runsync, /status, /stream, /cancel, /retry). Die zentrale strukturelle Verbesserung ist also nicht “etwas weniger Frickelei”, sondern ein anderer Failure-Mode: vom request-getriebenen Warm-Window hin zu queue-getriebenem Abarbeiten. 

Aber: ein kleiner lokaler Kontrollprozess bleibt trotzdem sinnvoll. Nicht mehr als Warm-up-/Backpressure-/Readiness-Orchestrator für jeden Request, sondern als Submit / Poll / Collect / Persist / Budget-Guard. Der Grund ist simpel: RunPod hält Ergebnisse nach Abschluss nur kurz vor — 30 Minuten für /run, 1 Minute für /runsync — und dokumentiert bei Queue-Based zwar Polling, Webhooks, TTL und manuelles Retry, aber keine dauerhaft gespeicherten Resultate. Wenn euer Client/Webhook in diesem Fenster ausfällt, ist der Plattformzustand nicht mehr eure Audit-Quelle. 

Unter den untersuchten Alternativen ist Baseten der einzige andere Kandidat, bei dem die Plattform einen ähnlichen Teil der Orchestrierung absorbieren kann: dedizierte Deployments, Async-Queue bis 72 Stunden, Queue auch bei 0 Replicas, regionale Endpoints mit expliziter EU-Routing-Garantie, Custom Docker und Weight-Caching. Der Preis dafür ist aber hoch: 24-GB-Klasse liegt öffentlich eher bei etwa $0.85/h für L4 und $1.21/h für A10G, also oberhalb eurer Zielklasse; außerdem sind Webhook-Lieferungen best-effort, und Baseten sagt ausdrücklich, dass Modell-Outputs nicht gespeichert werden, wenn der Webhook nach allen Retries scheitert. 

Modal scheidet für eure harte EU/EEA-Anforderung im derzeit öffentlich dokumentierten Produktbild faktisch aus: Zwar gibt es EU-Containerregionen und eine gute Job-Queue, aber Inputs für Modal Functions werden standardmäßig über Virginia (us-east) geroutet, routing_region ist separat und beta, und Inputs/Outputs größer als 2 MiB gehen weiterhin in Object Storage in us-east. Für gescannte Steuer-PDFs beziehungsweise Page-Images ist das kein defensibles Datenpfad-Modell. 

Was RunPod Queue-Based heute tatsächlich garantiert
RunPod dokumentiert Queue-Based-Endpunkte als Job-Lifecycle-API mit /runsync für synchrones Warten, /run für asynchrones Background-Processing, /status für Status und Resultat, /stream für inkrementelle Ausgabe, /cancel und /retry. Das ist nicht bloß syntaktisch ein anderer Zugang als euer heutiger LB-Endpunkt; die API ist konzeptionell darauf gebaut, dass ein Job nach Einreichung weiterlebt, auch wenn noch kein Worker bereitsteht. 

Die entscheidende Queue-Semantik steckt in TTL und Execution Timeout. Standardmäßig gilt ttl = 24h und executionTimeout = 10 min. ttl beginnt bei Einreichung und deckt Queue-Zeit plus Laufzeit ab; wenn TTL während Queueing oder Ausführung abläuft, wird der Job gelöscht. Beide Werte lassen sich pro Job setzen, bis maximal 7 Tage. Das heißt: Cold Starts von mehreren Minuten sind im QB-Modell kein strukturelles Problem, solange die TTL sauber über Queue- plus Laufzeit gelegt wird. Genau das dokumentiert RunPod selbst für “long-running jobs”. 

Für eure heutige LB-Schmerzstelle ist der wichtigste Unterschied daher: Im Queue-Based-Pfad gibt es keinen dokumentierten 2-Minuten-“waiting for worker”-Abwurf wie bei LB, sondern einen queued job, dessen Lebensdauer an TTL/Timeout hängt. Das ist der stärkste einzelne Befund in dieser Recherche, weil er euren lokalen Warm-up-Gatekeeper weitgehend überflüssig macht. Was lokal übrig bleibt, ist nicht Warten-auf-Warm, sondern Warten-auf-Ergebnis und sofortiges Persistieren. 

RunPod dokumentiert außerdem Webhooks: Ihr könnt beim Submit eine webhook-URL mitgeben; bei Fehlschlag retryt RunPod bis zu zwei weitere Male mit jeweils 10 Sekunden Abstand. Das ist brauchbar als Benachrichtigungsmechanismus, aber kein Dauer-Speicher. Dazu passt die kurze Result-Holding-Zeit: async 30 Minuten, sync 1 Minute. Für Tax-/Family-Office-Dokumente ist die richtige Architektur daher nicht “RunPod speichert Resultate”, sondern Worker schreibt große Artefakte selbst nach S3-kompatiblem Storage; Plattform-Webhooks und /status dienen nur als Zustandsleitung. RunPod dokumentiert genau für große Files den S3-kompatiblen Storage-Pfad mit s3Config; die Worker-Logik muss ihn selbst verwenden. 

Zu Retry und Worker-Ausfällen ist der Stand gemischt. Positiv: /retry requeued einen fehlgeschlagenen oder getimten Job mit gleicher Job-ID und gleichem Input; außerdem markiert das Worker-SDK Exceptions als FAILED. Negativ: In den öffentlich sichtbaren Docs fand ich keine harte, explizite at-least-once-Zusage für den Fall “Worker stirbt mitten im Job”. Es gibt die Werkzeuge, aber die exakte Mid-flight-Semantik bleibt ein Experiment, nicht ein dokumentiertes Versprechen. 

Was RunPod seit 2025 geändert hat und was davon euren Fall wirklich berührt
Für euren Stack sind drei Produktänderungen relevant. Erstens hat RunPod im Juni 2025 eine S3-kompatible API für Network Volumes angekündigt. Das ist wichtig, weil ihr große Inputs/Outputs und gegebenenfalls per-worker Datenpfade nicht mehr nur über Inline-JSON denken müsst. Zweitens kamen im September 2025 Cached Models in Beta: RunPod beschreibt das als Host-seitiges Vorplatzieren von Modellen, damit Worker bevorzugt auf Hosts landen, auf denen das Modell schon vorhanden ist. Das adressiert genau euren bekannten Anti-Pattern-Fall “Modell im 20-GB-Image backen”. Drittens dokumentiert RunPod im Juli 2026 Serverless Worker Fitness Checks, also automatische Health-Checks vor Jobannahme; das reduziert fehlerhafte Worker im Betrieb. 

Wichtig ist, was diese Neuerungen nicht lösen. Cached Models beseitigen laut Release Notes vor allem Model-Download-Zeit, nicht zwingend die gesamte vLLM-Initialisierung oder euren eigenen MinerU-/vLLM-/CUDA-Startpfad. Fitness Checks verbessern Zuverlässigkeit, aber sie ersetzen nicht euer Bedürfnis nach budgetierter, nachvollziehbarer Jobpersistenz. Und public docs zeigen FlashBoot in den 2026er Release Notes für CPU Serverless in Public Beta sowie Priority FlashBoot für Clusters — nicht klar für euren GPU-Queue-Based-Pfad. Für euren konkreten Pfad würde ich daher Cached Models/Storage ernst nehmen, FlashBoot aber nicht einplanen, solange ihr keine explizite GPU-QB-Dokumentation dafür seht. 

Die Worker-Seite ist inzwischen reifer als euer früherer Ausschlussgrund “handler-less image”. Das Python-SDK dokumentiert Standard-, Streaming-, Async- und Concurrent-Handler, Progress Updates, Worker Refresh und den Rat, schwere Initialisierung außerhalb des Handlers zu laden. Für euren Fall heißt das praktisch: ein Job = ein Dokument ist im Plattformmodell plausibel, und ihr könnt innerhalb eines Workers weiter entscheiden, ob ihr dokumentintern parallelisiert oder strikt sequentiell bleibt. Der frühere Einwand “QB nur weil Handler fehlt ungeeignet” ist nach heutigem Doku-Stand kein guter Einwand mehr. 

Kostenseitig ist RunPod Queue-Based zugleich besser und nüchterner als oft angenommen. Active workers sind dokumentiert als ständig warm und kontinuierlich kostenpflichtig, auch wenn sie idlen. Zusätzlich gibt es einen Idle Timeout, während dessen ein Worker nach Jobende warm bleibt und ebenfalls kostet; Standard ist 5 Sekunden. Für lange inaktive Endpunkte dokumentiert RunPod sogar eine automatische Absenkung des Endpunkts: nach 3 Tagen ohne Requests wird max workers auf 2 reduziert, nach 7 Tagen auf 0. Im öffentlich dokumentierten QB-Pfad habe ich keine separate “Standby warm pool”-Kostenfalle wie bei eurem LB-Erlebnis gefunden; die sichtbaren Kostenhebel sind vielmehr active workers und idle timeout. Das ist gut, aber kein Freibrief: Wer Warmth will, zahlt sichtbar dafür. 

Vergleich der realistischen Alternativen
Die Frage ist nicht nur “wer hat GPUs”, sondern “wer nimmt euch die lokale Orchestrierung wirklich ab, ohne eure EU-Anforderung oder euren Preisrahmen zu sprengen”. Der folgende Vergleich priorisiert genau diese Achsen. Alle Aussagen stützen sich auf öffentliche Produktdokumentation; offensichtliche Marketing-Claims habe ich bewusst nicht als Tie-Breaker verwendet. 

Plattform	EU-/Residency-Posture	Custom Image / eigener Stack	Queue- und Cold-Start-Semantik	Idle-Kosten	Öffentliche Preisnähe zur 16–24-GB-Klasse	Einschätzung
RunPod Queue-Based	EU-DC-Auswahl ist auf Endpoint-Ebene dokumentiert; RunPod behauptet GDPR-Compliance für Verarbeitung in europäischen Regionen. 
Ja, Queue-Based ist gerade für eigene Worker-Images/Handler gedacht. Async-, Streaming- und Concurrent-Handler sind dokumentiert. 
Jobs werden über /run//status verwaltet; TTL umfasst Queue+Run, max. 7 Tage. Kein LB-typischer 2-Minuten-Warteabwurf in der QB-Doku. Webhooks und /retry sind vorhanden. 
Bei active workers > 0 permanent kostenpflichtig; zusätzlich kostet Idle-Timeout-Warmth. Ohne aktive Worker skaliert der Endpoint herunter. 
Preis im von euch gewünschten Low-Cost-Segment hängt hier von GPU-Auswahl/Region ab; eure heutige RunPod-Nutzung zeigt, dass die Zielklasse real erreichbar ist. Öffentliche QB-GPU-Preisdetails wurden in dieser Recherche nicht vollständig extrahiert. 
Konservativ beste Option. Plattform absorbiert den meisten Orchestrierungsaufwand. Es bleibt nur ein kleiner lokaler Kontrollprozess.
Baseten	Stark, wenn Regional Environments konfiguriert sind: regionale Endpoints routen direkt in die gewählte Region; Standard-Endpoints tun das ausdrücklich nicht. Unterstützte Regionen umfassen u. a. EU. 
Ja. Custom Docker, Private Registry, sogar no_build für unveränderte Images; BDN funktioniert auch mit Custom Docker. 
Async inference mit Queue bis 72h, Laufzeit bis 1h; Requests queue’n auch bei 0 Replicas. Sync-Requests werden geparkt; async hat definierte Statuscodes und Queue-Status. 
min replicas = 0 bedeutet keine Idle-Kosten; Model-Load auf frischer Replica wird metered. 
Öffentliche Preise liegen oberhalb eures Sweet Spots: T4 ~$0.63/h, L4 ~$0.85/h, A10G ~$1.21/h. 
Technisch stark, preislich schwach für euren Korridor. Eher Enterprise-Alternative als Low-Cost-Ersatz.
Modal	Für strikte EU-Verarbeitung im öffentlichen Doku-Stand nicht defensibel: standardmäßig gehen Inputs über Virginia; routing_region ist separat, beta, und Inputs/Outputs >2 MiB landen weiterhin in us-east. 
Ja. Registry-Images, Dockerfiles, Cloud bucket mounts sind dokumentiert. 
Sehr gute Job-Queue (.spawn()), bis 1 Mio. Jobs, Resultate bis 7 Tage, Autoscaling auf 0, Retries/Preemption. 
Keine Idle-Kosten laut Pricing; nur Computezeit. 
L4 ~$0.80/h, A10 ~$1.10/h, T4 ~$0.59/h. 
Datenschutzseitig disqualifiziert für euren Fall, obwohl die Job-Orchestrierung sehr gut ist.
Beam Cloud	Europa-Regionen existieren, aber die Doku sagt nur: “wenn ihr eine bestimmte Region wollt, meldet euch”; das ist für strikte Residency zu weich. Beam bietet allerdings auch Self-Hosting, bei dem alles in eurer Umgebung laufen kann. 
Ja. Base-Image, Dockerfile, Custom Registry, eigener CUDA-Stack sind dokumentiert. 
Task Queues sind nativ, liefern Task-ID, Polling oder Callback, und retryen standardmäßig 3-mal. Dokumentation ist aber dünner bei TTL/Retention als RunPod/Baseten. 
Scale-to-zero by default; keine Charges für Machine Start oder Image Pull, nur laufende Container und keep_warm_seconds. 
Öffentliche Serverless-Preise: RTX 4090 ~$0.69/h, A10G ~$1.05/h; keine offizielle L4-Serverless-Zahl in den gesichteten Quellen. 
Interessant, aber für Managed nicht residency-scharf genug. Self-hosted Beam könnte euren lokalen Orchestrator stark schrumpfen lassen, ist dann aber faktisch keine vollständig “fremd gemanagte” Escape Route mehr.
Koyeb	Besser als viele Konkurrenten: EU-Regionen wie Frankfurt und Paris sind dokumentiert; die DPA sagt, Kundendaten bleiben in der gewählten Region. 
Ja, Docker-Image-Deploy ist offiziell dokumentiert. 
Serverless GPU/Container mit Scale-to-zero; in den gesichteten offiziellen Docs keine native durable Job-Queue wie RunPod QB oder Baseten Async. 
Scale-to-zero für GPU ist dokumentiert; Idle-Period standardmäßig 5 Minuten. 
Öffentliche GPU-Preisdetails wurden in dieser Recherche nicht sauber bis zur passenden 24-GB-Klasse extrahiert. 
Guter Container-Serverless-Baustein, aber kein Orchestrator-Ersatz. Ihr würdet sehr wahrscheinlich wieder selbst queue’n müssen.
Replicate	In den geprüften öffentlichen Docs fand ich keine belastbare EU-Region-/Residency-Garantie für euren Fall. 
Custom Models ja, aber im Replicate-eigenen Deployment-/Packaging-Modell; kein klarer BYO-Docker-Region-Story-Befund in den geprüften Docs. 
Webhooks/Predictions sind vorhanden; als queue-native Batch-Platform für strikte Residency wirkte sie in der geprüften Doku nicht passend. 
Pay-per-use. 
T4 ~$0.81/h laut Pricing-Übersichten rund um Replicate; H100 deutlich höher. 
Für euren Fall nicht defensibel genug.
Inferless	Nicht mehr als eigenständige Alternative zu bewerten: acqui-hire durch Baseten im Februar 2026. 
Hatte Custom Runtime; praktisch geht die Bewertung nun in Baseten auf. 
War queue-/serverless-orientiert, aber als eigenständige Option in 2026 obsolet. 
—	—	Nicht mehr separater Kandidat.

Der klare Befund aus diesem Vergleich ist: Nur RunPod QB und Baseten nehmen euch den Job-Orchestrierungs-Kern wirklich ab. Modal wäre technisch ebenfalls stark, fällt aber an eurer Datenpfad-Anforderung. Beam und Koyeb sind gute Serverless-Container-Plattformen, aber in den öffentlich sichtbaren Eigenschaften nicht dieselbe Klasse “durable async batch queue mit Residency-Story”. 

Was damit für eure Architektur folgt
Wenn ihr bei RunPod bleibt, aber auf Queue-Based wechselt, dann sollte die Architekturfrage nicht mehr lauten “brauchen wir einen Orchestrator?”, sondern “wie klein darf der lokale Kontrollprozess werden?”. Meine Antwort: sehr klein. Ein sinnvoller Minimalumfang ist

Job-Einreichung pro Dokument via /run,
Polling oder Webhook-Empfang,
sofortiges Persistieren von Ergebnissen und Logs in euren eigenen Storage,
Idempotenzschicht auf Dokumentebene,
Budget-/Concurrency-Grenzen auf eurer Seite, obwohl RunPod max workers und Rate Limits hat. 
Was ihr ziemlich sicher nicht mehr lokal bauen müsst, wenn QB sauber funktioniert: Queue-until-warm, separate Readiness-Gates, Vorwärm-Workflows, Burst-Backpressure gegen einen LB, der wartende Requests nach 2 Minuten abschneidet. Diese Logik existiert heute, weil ihr einen request-synchronen Pfad auf ein cold-start-lastiges System zwingt. QB dreht genau diese Kopplung um. 

Was ihr weiterhin lokal besitzen solltet, sind Dinge, die Plattformen fast nie sauber genug liefern: Auditierbarkeit, persistente Ergebnisbuchführung, kostenseitige Kill-Switches und document-level idempotency. Das gilt sogar bei Baseten, obwohl deren Async-Queue stark ist, weil Baseten ausdrücklich sagt, dass Model-Outputs nicht gespeichert werden und bei Webhook-Failures verloren gehen können. Wenn schon Baseten das offen so sagt, sollte man denselben Grundsatz auch für RunPod anwenden: Plattform-Queue ja, Plattform als Source of Truth nein. 

Endgültiges Votum
Mein ehrliches Votum lautet:

Die Plattform kann die Orchestrierung weitgehend absorbieren — aber nur im RunPod-Queue-Based-Modell, und nur wenn ihr den lokalen Teil radikal auf “submit / poll-or-webhook / collect / persist / budget guard” zusammenschneidet. 

Ich würde nicht den bisherigen Plan eines dicken, dauerhaften Single-Owner-Orchestrators weiterziehen, der Warm-up, Queueing, Parking, Readiness, Burst-Drosselung und Crash-Recovery selbst modelliert. Das wäre unter den heute dokumentierten Queue-Based-Eigenschaften Doppelarbeit gegen die Plattform. Ihr würdet damit wieder ein eigenes Scheduling-System bauen, obwohl RunPod inzwischen genau dafür eine passende API hat. 

Ich würde aber ebenso nicht auf “gar keinen lokalen Kontrollprozess” gehen. Dafür sind die dokumentierten Result- und Webhook-Semantiken zu kurzlebig. Praktisch heißt das: kein Orchestrator als Betriebsersatz, aber ein dünner, robuster Job-Controller als Daten- und Kostenhülle. Das ist eine sehr andere Größenordnung als euer aktueller Architekturimpuls. 

Wenn ihr bereit seid, deutlich mehr pro GPU-Stunde zu zahlen, ist Baseten die einzige echte Alternativplattform, bei der ich sagen würde: ja, auch dort kann die Plattform den Orchestrierungs-Kern absorbieren. Wenn ihr am Preisfenster und an echter EU-Verteidigbarkeit festhaltet, bleibt RunPod QB der wahrscheinlich beste maintained path. 

Offene Punkte, die nur ein billiger Test sauber klärt
Die verbleibenden Unbekannten sind klein genug, dass man sie nicht theoretisch erschlagen sollte. Ein billiger, gezielter Testplan reicht. Die wichtigsten Experimente sind diese:

Kalter 20-GB-QB-Worker mit min workers = 0: Einen echten Dokumentjob an einen komplett kalten Queue-Based-Endpoint schicken und prüfen, ob der Job über einen 8–10-Minuten-Cold-Start sauber in QUEUED bleibt und danach normal läuft. Dokumentiert ist, dass TTL Queue-Zeit abdeckt; nicht dokumentiert ist euer exakter End-to-End-Pfad mit so fettem Image. 
Mid-job-Worker-Tod: Einen Worker absichtlich während eines Dokuments crashen lassen oder OOM provozieren. Messen: finaler Status, Sichtbarkeit in /status, Verhalten von /retry, möglicher Doppelanlauf, Idempotenzbedarf. Die public docs geben dafür Werkzeuge, aber kein hartes at-least-once-Versprechen. 
Große Outputs nur über Objekt-Storage: Nicht Resultate über /status missbrauchen, sondern MinerU-Output direkt in S3-kompatiblen Storage schreiben. Testen, wie viel Metadaten ihr trotzdem über /status transportieren wollt. RunPods eigener “sanctioned pattern” für große Dateien ist S3-kompatibler Storage. 
Cached Models statt baked image: Prüfen, wie viel Cold-Start ihr wirklich durch Host-seitiges Model-Caching spart. Die Release Notes versprechen Eliminierung von Download-Zeit, nicht automatisch von vLLM-Init oder eurem kompletten Startpfad. 
Kostenverhalten ohne Warm-Halte-Tricks: QB-Endpoint mit active workers = 0, kurzer Idle-Time und realistischem Burst laufen lassen; danach Rechnung prüfen. Die Doku ist klar zu active workers und idle timeout, aber euer früheres LB-Erlebnis rechtfertigt einen Verifikationstest. 
Dokumentgranularität: Ein Job pro Dokument ist naheliegend. Ob für euch stattdessen ein Job pro Dokument-Chunk oder pro Seitenblock ökonomisch besser ist, entscheidet nicht die Doku, sondern euer reales Throughput-/Restart-/Failure-Profil. Die Plattform erlaubt beides; die richtige Einheit ist eine Workload-Frage. 
Das eigentliche strategische Ergebnis der Recherche ist damit klar: Nicht “großen lokalen Orchestrator bauen”, sondern “auf Queue-Based wechseln und nur den dünnen Kontrollring behalten”. Für euren Stack ist das derzeit der robusteste, günstigste und am wenigsten selbstgebaut wirkende Weg. 
