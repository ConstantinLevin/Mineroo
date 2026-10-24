# Procrastinate-Feature-Verifikation gegen die offizielle Dokumentation

**Datum:** 2026-08-06
**Geprüfte Version / Doku-Stand:** Procrastinate stable-Doku auf readthedocs; der neueste Changelog-Eintrag der stable-Doku ist Version 3.9.0 (geprüft am 2026-08-06 über https://procrastinate.readthedocs.io/en/stable/changelog.html). Das deckt die Zielversion 3.9.x ab.
**Zweck:** Der Plan für den OCR-Service (siehe `plans/2026-08-05-ocr-service-plan.md`: Producer legen Dokument-Zeilen in Postgres an und deferren je Dokument einen Procrastinate-Job; ein Daemon-Prozess mit async Tasks arbeitet sie ab, spricht mit der RunPod-HTTP-API und schreibt Ergebnisse in eine eigene `documents`-Tabelle) stützt sich auf 17 Feature-Behauptungen über Procrastinate. Jede wurde einzeln gegen die offizielle Doku geprüft. Am Abend des 06.08. sind zwei nachgezogene Punkte (18–19) hinzugekommen — ausgelöst durch die „ein Buch, nicht zwei"-Umstellung des Plans; sie sind teils gegen die Doku, teils **am Objekt** (Quellcode und SQL-Schema der installierten 3.9.0) geprüft, siehe den Quellen-Nachtrag unten.

**Benutzte Quellen:**

- Context7-Library-ID: `/websites/procrastinate_readthedocs_io_en_stable` (offizielle readthedocs-Doku, per `resolve-library-id` aufgelöst; Alternative `/procrastinate-org/procrastinate` nicht benutzt).
- Direkt geladene Doku-Seiten (jeweils `https://procrastinate.readthedocs.io/en/stable/` + Pfad), vollständig gelesen:
  - `howto/production/retry_stalled_jobs.html`
  - `howto/advanced/cron.html`
  - `howto/advanced/queueing_locks.html`
  - `howto/advanced/schedule.html`
  - `howto/advanced/sync_defer.html`
  - `howto/advanced/events.html`
  - `howto/advanced/retry.html`
  - `howto/basics/worker.html`
  - `howto/basics/batch_defer.html`
  - `howto/production/monitoring.html`
  - `howto/django/models.html`
  - `discussions.html`
  - `glossary.html`
  - `reference.html` (gezielt durchsucht, nicht vollständig gelesen — die Seite ist der API-Gesamtindex; zitierte Stellen wurden im Kontext gelesen)
  - `changelog.html` (nur Versionsermittlung)
  - `searchindex.js` (Volltext-Suchindex der gesamten stable-Doku; benutzt für Negativ-Befunde: welche Wörter in der Doku nirgends vorkommen)
- Keine weiteren Quellen (kein Stack Overflow, keine Blogs, kein Quellcode). — Gilt für die Punkte 1–17.
- **Quellen-Nachtrag 06.08. (nur Punkte 18–19):** `howto/advanced/locks.html` vollständig gelesen; außerdem — bewusst abweichend vom obigen „kein Quellcode" — der Quellcode der INSTALLIERTEN Procrastinate 3.9.0 im Miniconda-Base als Objekt-Beleg: `/opt/homebrew/Caskroom/miniconda/base/lib/python3.12/site-packages/procrastinate/sql/schema.sql` (Funktion `procrastinate_retry_job_v2`), `sql/queries.sql` (Retry-Aufruf), `manager.py` (Routing `retry_job` → `retry_job_by_id_async`), `shell.py` (Shell-Kommandos). Der Objekt-Beleg steht hier über dem Doku-Beleg, weil er die tatsächlich laufende Version zeigt.

**Lesehinweis zu den Befunden:** BESTÄTIGT = die Behauptung stimmt so, wie sie im Plan steht. PRÄZISIERT = die Behauptung stimmt im Kern, aber ein Detail weicht ab oder muss enger gefasst werden; der korrigierte Sachverhalt steht dabei. WIDERLEGT = die Behauptung ist falsch. NICHT BELEGT = die Doku gibt dazu nichts her; es wird nicht geraten, sondern der Punkt muss am Objekt (mit laufendem Procrastinate gegen eine echte Datenbank) getestet werden.

---

## 1. Queues als Isolationsmittel

**Behauptung:** Tasks können festen Queues zugeordnet werden (`@app.task(queue=...)`), ein Worker kann auf eine Teilmenge von Queues beschränkt werden; Zusatzfrage: zwei Worker parallel in einem Prozess (zwei `run_worker_async`-Koroutinen, z. B. Queue "ocr" mit concurrency=24 und Queue "maintenance" mit concurrency=1), damit Wartungs-Tasks nicht verhungern.

**Befund:** Hauptteil BESTÄTIGT; Zusatzfrage NICHT BELEGT.

**Beleg:**
- Queue-Zuordnung: Der `@app.task`-Decorator hat den Parameter `queue`: "The name of the queue in which jobs from this task will be launched, if the queue is not overridden at launch. Default is `\"default\"`." (reference.html, `task` decorator). Beispiel `@app.task(queue="sums")` auf der Startseite (index.html).
- Worker-Beschränkung: `App.run_worker_async` hat den Parameter `queues`: "List of queues to listen to, or None to listen to every queue (defaults to `None`)" (reference.html). Ebenso CLI: `procrastinate worker [queue [...]]` — "not specifying queues will tell Procrastinate to listen to every queue" (howto/basics/worker.html).
- Worker in einem größeren Prozess: dokumentiert ist EIN Worker als asyncio-Task innerhalb einer Anwendung — "When running the worker inside a bigger application, you may want to use `install_signal_handlers=False` so that the worker doesn't interfere with your application's signal handlers", mit FastAPI-Beispiel `worker = asyncio.create_task(task_queue.run_worker_async(install_signal_handlers=False))` (howto/basics/worker.html).
- Zwei Worker in EINEM Prozess: dafür gibt es in der gesamten stable-Doku weder ein Beispiel noch eine Aussage (dafür oder dagegen). Die Concurrency-Seite beschreibt als dokumentiertes Modell: "To increase processing throughput, you can launch multiple workers, each using their own process. Procrastinate workers can also launch multiple asynchronous sub-workers to leverage concurrency" (howto/production/concurrency.html, via Context7) — also mehrere Worker = mehrere Prozesse, und Parallelität INNERHALB eines Workers = `concurrency`-Sub-Worker. Eine per-Queue-Concurrency innerhalb eines Workers ist nicht dokumentiert.

**Konsequenz für den Plan:** Queue-Trennung und Queue-Beschränkung tragen wie geplant; die Zwei-Koroutinen-Konstruktion ist unbelegtes Terrain und muss am Objekt getestet werden (oder der Plan weicht auf das dokumentierte Muster aus: zwei Prozesse, oder ein Worker für beide Queues — Letzteres löst das Verhungerungsproblem aber nicht, weil alle Jobs eines Workers denselben `concurrency`-Pool teilen, siehe Punkt 2).

---

## 2. Periodische Tasks teilen den Slot-Pool

**Behauptung:** `@app.periodic(cron=...)` deferrt Jobs, die vom normalen Worker ausgeführt werden — sie konkurrieren um dieselben Concurrency-Slots wie alle anderen Jobs derselben Queue-Auswahl.

**Befund:** BESTÄTIGT.

**Beleg:** Die Cron-Seite sagt unter "Caveats" wörtlich: "Tasks are periodically deferred but their execution time depends on the state of your queue. If your workers are overwhelmed with long tasks, periodic executions could be delayed. One possible solution involves using queues to make sure short tasks are not delayed by long tasks." (howto/advanced/cron.html). Periodische Tasks sind gewöhnliche Tasks (`@app.periodic` wird ÜBER `@app.task` gestapelt, siehe das `retry_stalled_jobs`-Beispiel in howto/production/retry_stalled_jobs.html), deren Deferral die Worker selbst auslösen: "Workers are responsible for deferring periodic tasks. If there is no worker running, then periodic tasks will not get scheduled. On the other hand, even if you have multiple workers, periodic tasks will only be deferred once per period." (howto/advanced/cron.html).

**Konsequenz für den Plan:** Trägt wie geplant — und die Doku empfiehlt für genau dieses Problem die Queue-Trennung, die der Plan vorsieht; ungelöst bleibt nur, wie die getrennte Queue eigene Slots bekommt (Punkt 1).

---

## 3. Abort-Semantik für async-Tasks

**Behauptung:** `cancel_job_by_id(<id>, abort=True)` signalisiert einem laufenden Job den Abbruch; bei async-Tasks wird die Koroutine gecancelt (`CancelledError`) — eine `asyncio.sleep`-Warteschleife ist von Haus aus abbrechbar. Wie ist die Mechanik (auch sync — `context.should_abort()`?), und was gilt bei `shutdown_graceful_timeout`?

**Befund:** BESTÄTIGT.

**Beleg:**
- Markieren: "Mark a job for abortion using `cancel_job_by_id` with `abort=True`. If the job is already running, this signals the worker to abort." — `app.job_manager.cancel_job_by_id(33, abort=True)` bzw. `await app.job_manager.cancel_job_by_id_async(33, abort=True)` (howto/advanced/cancellation.html). Referenz-Semantik: "abort (bool) – If True, a job will be marked for abortion, but the task itself has to respect the abortion request. If False, only jobs in todo state will be set to cancelled and won't be processed by a worker anymore. […] Returns: If True, the job was cancelled (or its abortion was requested). If False, nothing was done: either there is no job with this id or it's not in a state where it may be cancelled (i.e. todo or doing)" (reference.html, `JobManager.cancel_job_by_id`).
- Async-Mechanik: "Asynchronous tasks are cancelled via asyncio's cancellation mechanism. If a job is aborted while awaiting an operation, `asyncio.CancelledError` will be raised." (howto/advanced/cancellation.html). Ein Job, der in `await asyncio.sleep(...)` wartet, wird also genau dort mit `CancelledError` geweckt. Der Zustandsübergang: "An async job handles it automatically by internally raising a CancelledError exception." (discussions.html, Zustand g: doing → aborted).
- Sync-Mechanik: "In a synchronous task, periodically check `context.should_abort()` and raise `exceptions.JobAborted` to respect the abortion request." (howto/advanced/cancellation.html); ebenso discussions.html: "A sync job must also handle the abort request by checking `context.should_abort()` and raising a `JobAborted` exception."
- Transport des Abort-Signals: Der Worker erfährt von Abort-Requests per LISTEN/NOTIFY ("listen_notify — If True, allocates a connection from the pool to listen for: new job availability, job abort requests", reference.html) mit Poll-Fallback: "when listen_notify=False or the abort notification was missed, abort_job_polling_interval will represent the maximum delay before the worker reacts to an abort request. Note that the worker will only poll the database for abort requests when at least one job is running." (discussions.html).
- Shutdown: "shutdown_graceful_timeout — Indicates the maximum duration (in seconds) the worker waits for jobs to complete when requested to stop. Jobs that have not been completed by that time are aborted. A value of None corresponds to no timeout. (defaults to None)" (reference.html). Beispiel in howto/advanced/shutdown.html: `worker.cancel()` mit `shutdown_graceful_timeout=10` — "Any job that took longer than 10 seconds to complete have aborted". Der Changelog zu 3.0.0 fasst die Mechanik zusammen: "Aborted asynchronous jobs are now cancelled using asyncio's cancel method, allowing for graceful handling with shielding. Synchronous jobs will not be cancelled directly but will return True from job_context.should_abort() […] A new graceful stopping mechanism with shutdown_graceful_timeout is implemented, which requests abortion for both synchronous and asynchronous jobs during shutdown." (changelog.html, Abschnitt 3.0.0).

**Konsequenz für den Plan:** Trägt wie geplant — async-Warteschleifen (RunPod-Polling per `asyncio.sleep`) sind ohne Zusatzcode abbrechbar; beim Shutdown greift dieselbe Abort-Mechanik nach Ablauf des Timeouts.

---

## 4. At-least-once / Idempotenz-Erwartung

**Behauptung:** Ein Task kann mehr als einmal ausgeführt werden (Retries, stalled-Wiederaufnahme), und die Doku erwartet/empfiehlt idempotente Tasks. Wo steht das?

**Befund:** Erster Teil PRÄZISIERT (Mehrfachausführung ist real und aus der Doku ableitbar, steht aber nirgends als ausdrückliche Garantie-Aussage); zweiter Teil NICHT BELEGT — eine Idempotenz-Empfehlung steht in der stable-Doku NICHT.

**Beleg:**
- Negativ-Befund, geprüft am Volltext-Suchindex der gesamten stable-Doku (`searchindex.js`, alle 58 Seiten): Das Wortfragment "idempot" kommt 0-mal vor; "duplicate" 0-mal; "at least once" 0-mal; "twice" genau 1-mal, und zwar im Glossar in einem Beispiel zur Unterscheidung Task/Job ("If you did the laundry once and the dishes twice…", glossary.html) — ohne Bezug zu Mehrfachausführung.
- Dass Jobs mehrfach laufen können, ergibt sich aus dokumentierten Mechanismen: (a) Retries führen denselben Job erneut aus — "Retry 5 times (so 6 attempts total)" (howto/advanced/retry.html) und `Job.attempts`: "Number of times the job has been tried." (reference.html); (b) die stalled-Wiederaufnahme (Punkt 6) legt `doing`-Jobs gestorbener Worker erneut vor; die Doku warnt beim veralteten `nb_seconds`-Parameter ausdrücklich, dass er "may lead to wrongly retrying jobs that are still running" (howto/production/retry_stalled_jobs.html) — d. h. die Doku kennt den Fall, dass ein noch laufender Job erneut vorgelegt wird und dann doppelt läuft.

**Konsequenz für den Plan:** Die Plan-Anforderung „Tasks idempotent bauen" bleibt inhaltlich richtig und nötig (Mehrfachausführung ist real), aber der Plan darf sie nicht als Doku-Zitat begründen — die Begründung ist die Mechanik (Retries + stalled-Wiederaufnahme), nicht eine Doku-Empfehlung.

---

## 5. queueing_lock

**Behauptung:** Verhindert, dass zwei Jobs mit demselben `queueing_lock` gleichzeitig WARTEN (`AlreadyEnqueued` beim zweiten defer). Kernfrage: Zählt ein Job im Zustand `doing`, oder nur `todo`? Wird der Lock bei terminalen Zuständen freigegeben?

**Befund:** BESTÄTIGT, mit einer wichtigen Präzisierung zur Freigabe: Der Lock ist bereits frei, sobald der Job `todo` VERLÄSST — also schon bei `doing`, nicht erst bei terminalen Zuständen.

**Beleg:**
- Kernsatz wörtlich: "queueing_lock allows a single job in todo status. Meanwhile, it allows multiple jobs to be in doing status." (howto/advanced/queueing_locks.html). Ein laufender (`doing`) Job zählt also NICHT.
- Exception: "Trying to defer a task when the queueing_lock is active will result in an AlreadyEnqueued exception, which you can choose to ignore." (howto/advanced/queueing_locks.html); Referenz: "exception procrastinate.exceptions.AlreadyEnqueued — There is already a job waiting in the queue with the same queueing lock." (reference.html). Attribut-Definition: "queueing_lock: No two jobs with the same queueing lock can be waiting in the queue." (reference.html, `Job.queueing_lock`).
- Freigabe: folgt direkt aus dem Kernsatz — der Lock bindet ausschließlich den `todo`-Status; jeder Übergang aus `todo` heraus (nach `doing` oder nach `cancelled`) gibt ihn frei; terminale Zustände sind erst recht frei. Eine eigene „Freigabe"-Aussage macht die Doku nicht; sie ist logische Folge des zitierten Satzes.
- Wenn zusätzlich „höchstens einer läuft" gebraucht wird: "To enforce that only one job runs at a time while limiting the queue size, you can combine queueing_lock with lock." (howto/advanced/queueing_locks.html).

**Konsequenz für den Plan:** Trägt wie geplant für „kein zweites Warten"; falls der Plan an irgendeiner Stelle stillschweigend annimmt, dass ein LAUFENDER Job ein erneutes defer blockiert, muss dort zusätzlich `lock` (nicht `queueing_lock`) eingesetzt werden.

---

## 6. Stalled-Job-Wiederaufnahme

**Behauptung:** Worker-Heartbeats (`update_heartbeat_interval`), `stalled_worker_timeout`, `job_manager.get_stalled_jobs()` + `retry_job()` als dokumentiertes Muster. Was gibt `get_stalled_jobs` zurück, was tut `retry_job` (gleicher Job? attempts-Zähler?)?

**Befund:** BESTÄTIGT; Teilfrage attempts-Zähler beim stalled-Retry war in der Doku NICHT BELEGT — inzwischen am Objekt beantwortet (Punkt 19): jedes Zurücksetzen auf `todo` erhöht `attempts`, auch die stalled-Wiederaufnahme.

**Beleg:**
- Mechanik wörtlich: "Procrastinate workers update heartbeats at a regular interval (every 10 seconds by default). If a worker is terminated without a regular shutdown, the heartbeat of that worker will not be updated, and the worker will be considered stalled. Jobs in the doing state of such stalled workers are considered stalled as well and can be fetched by the with the JobManager.get_stalled_jobs() method." (howto/production/retry_stalled_jobs.html). Worker-Parameter: `update_heartbeat_interval` (Default 10 s), `stalled_worker_timeout` (Default 30 s; "A worker prunes stalled workers from the database at startup") (reference.html, `App.run_worker_async`).
- `get_stalled_jobs`: "Return all jobs that have been in `doing` state for more than a given time." Rückgabetyp `Iterable[Job]`; Parameter `queue`, `task_name` (Filter) und `seconds_since_heartbeat` (Default 30 s): "Get stalled jobs based on workers that have not sent a heartbeat for longer than this time in seconds." Der alte Parameter `nb_seconds` ist deprecated, "as it may lead to wrongly retrying jobs that are still running" (reference.html + howto/production/retry_stalled_jobs.html).
- Dokumentiertes Muster (wörtlich aus howto/production/retry_stalled_jobs.html):
  ```python
  @app.periodic(cron="*/10 * * * *")
  @app.task(queueing_lock="retry_stalled_jobs", pass_context=True)
  async def retry_stalled_jobs(context, timestamp):
      stalled_jobs = await app.job_manager.get_stalled_jobs()
      for job in stalled_jobs:
          await app.job_manager.retry_job(job)
  ```
- `retry_job(job, retry_at=None, priority=None, queue=None, lock=None)`: "Indicates that a job should be retried later." (reference.html). Es ist derselbe Job (das Job-Objekt wird übergeben, kein neuer Job erzeugt); die Retry-Seite formuliert für den verwandten manuellen Fall: Status zurück auf `todo` bei erhaltener Event-Historie (siehe Punkt 7). Ob der `attempts`-Zähler beim stalled-Retry erhöht wird, sagt die Doku nicht; dokumentiert ist nur die Definition "attempts: Number of times the job has been tried." (reference.html, `Job.attempts`).

**Konsequenz für den Plan:** Trägt wie geplant; ob stalled-Wiederaufnahmen auf `max_attempts` der Retry-Strategie anrechnen, ist inzwischen am Objekt beantwortet: ja (Punkt 19).

---

## 7. Manuelles Retry GESCHEITERTER (failed) Jobs

**Behauptung:** Die Doku-Seite "Retry a Job Manually" — funktioniert ein Bordmittel auch für Jobs im Endzustand `failed`? (Der Plan verlässt sich darauf: „failed-Dokumente werden mit Procrastinate-Bordmitteln erneut vorgelegt".)

**Befund:** BESTÄTIGT.

**Beleg:** Wörtlich von der Seite "Retry a Job Manually" (Abschnitt auf howto/advanced/retry.html): "Sometimes a manual retry, for instance, after we fix an integration's configuration, can be practical. This is why the job_manager offers an API to do so. Retrying a failed job will set the status of the job back to todo while keeping the history of events in place. The action of retrying a failed job, is also recording a new Event of type retried." Programmatisch:
```python
app.job_manager.retry_job_by_id(
    job.id, utils.utcnow(), job.priority, job.queue_name, job.lock
)
# or by using the async method
await app.job_manager.retry_job_by_id_async(
    job.id, utils.utcnow(), job.priority, job.queue_name, job.lock
)
```
Referenz-Signatur: `retry_job_by_id(job_id, retry_at, priority=None, queue=None, lock=None)` — "retry_at: The time at which the job should be retried. If set at present time or in the past, the job may be retried immediately." (reference.html). Zusätzlich existiert im Admin-Shell-Werkzeug ein `retry`-Kommando ("commands to retry (retry) & cancel (cancel) a specific job", howto/production/monitoring.html) und für Django eine Admin-Action "Retry Failed Job" (howto/advanced/retry.html).

Anzumerken ist ein Widerspruch innerhalb der Doku: Die Retry-Seite nennt einen Event-Typ `retried`; die Events-Seite (howto/advanced/events.html, siehe Punkt 11) listet diesen Typ in ihrer Aufzählung der Ereignistypen nicht auf. Welche der beiden Seiten den tatsächlichen Datenbestand beschreibt, lässt sich aus der Doku nicht entscheiden.

**Konsequenz für den Plan:** Trägt wie geplant — `retry_job_by_id`/`retry_job_by_id_async` ist genau das Bordmittel für failed-Dokumente; derselbe Job wird wieder `todo`, die Event-Historie bleibt. Die genaue Zustandsmenge, die die Funktion akzeptiert (`doing|failed`, ausdrücklich NICHT `aborted`), und das `attempts`-Verhalten stehen — am Objekt geprüft — in Punkt 19.

---

## 8. Custom-Retry-Strategie

**Behauptung:** `BaseRetryStrategy.get_retry_decision(exception, job)` → `RetryDecision(retry_in/retry_at, priority, queue, lock, should_retry)`. (a) Erhöht jeder Retry `job.attempts`? (b) Kann die Strategie nach Exception-TYP unterscheiden? (c) Gibt es `retry_exceptions` auf Task-Ebene?

**Befund:** BESTÄTIGT, mit einer Präzisierung zu `should_retry`.

**Beleg:**
- Referenz: "class procrastinate.BaseRetryStrategy — If you want to implement your own retry strategy, you can inherit from this class. Child classes only need to implement `get_retry_decision`." Signatur: `get_retry_decision(exception: BaseException, job: Job) → RetryDecision | None` — "Returns: RetryDecision | None - Specifies when and how a job should be retried, or None if the job should not be retried." (reference.html). Howto: "If None is returned from get_retry_decision then the job will not be retried." (howto/advanced/retry.html).
- `RetryDecision`-Parameter laut Referenz: `retry_at` ("If set at present time or in the past, the job may be retried immediately. […] Defaults to present time."), `retry_in`, `priority`, `queue`, `lock` (reference.html). Präzisierung: Ein Parameter `should_retry` steht NICHT in der Referenz-Signatur; er erscheint ausschließlich im Howto-Codebeispiel (`return RetryDecision(should_retry=False)`, howto/advanced/retry.html). Der doppelt abgesicherte, in Referenz UND Howto dokumentierte Weg, einen Retry zu verweigern, ist `return None`.
- (a) attempts: "Retry 5 times (so 6 attempts total)" (howto/advanced/retry.html); `Job.attempts`: "Number of times the job has been tried." (reference.html); die (deprecated) `get_schedule_in`-Doku definiert: "attempts — The number of previous attempts for the current job. The first time a job is run, attempts will be 0." (reference.html). Das Howto-Beispiel entscheidet selbst über `if job.attempts >= max_attempts`. Jeder Ausführungsversuch zählt also hoch — ja.
- (b) Exception-Typ: `get_retry_decision` erhält die geworfene Exception als Argument (Signatur oben) — die Strategie kann per `isinstance` transient von terminal unterscheiden. Ja.
- (c) `retry_exceptions`: "define the exception types you want to retry on (if you don't, jobs will be retried on any type of exceptions)" — `@app.task(retry=procrastinate.RetryStrategy(max_attempts=10, wait=5, retry_exceptions={ConnectionError, IOError}))` (howto/advanced/retry.html); Referenz-Signatur `RetryStrategy(*, max_attempts=None, wait=0, linear_wait=0, exponential_wait=0, retry_exceptions=None)` mit Warteformel "total_wait = wait + lineal_wait * attempts + exponential_wait ** (attempts + 1)" (reference.html). Ja — auf Task-Ebene über den `retry`-Parameter des Task-Decorators.

**Konsequenz für den Plan:** Trägt wie geplant; im eigenen Code für „kein Retry" `return None` verwenden (statt `should_retry=False`), weil nur das in der Referenz zugesichert ist.

---

## 9. batch_defer

**Behauptung:** Mehrere Jobs eines Tasks in einem Aufruf einreihen (sync und async). Limits? Wechselwirkung mit `queueing_lock` (was, wenn EINER der Einträge `AlreadyEnqueued` wäre)?

**Befund:** BESTÄTIGT; ein Mengen-Limit ist nicht dokumentiert; die queueing_lock-Wechselwirkung ist dokumentiert und strenger als vielleicht erwartet: alles-oder-nichts mit vollem Rollback.

**Beleg:**
- API: "Instead of deferring each job one by one, you can defer multiple jobs at once using the batch_defer resp. batch_defer_async method." — `my_task.batch_defer({"a": 1, "b": 2}, {"a": 3, "b": 4}, ...)` bzw. `await my_task.batch_defer_async(...)`; mit `configure(...)` kombinierbar; Iterable per `*payloads` entpackbar (howto/basics/batch_defer.html). Auf JobManager-Ebene: `batch_defer_jobs(jobs, connection=None)` / `batch_defer_jobs_async` — "Add multiple jobs in their queue for later processing by a worker. […] Returns: A list of jobs with their id set." (reference.html).
- Limits: Weder die Howto-Seite noch die Referenz nennen eine Obergrenze für die Anzahl der Payloads. (Abwesenheit einer Angabe — nicht die Behauptung, es gäbe kein Limit.)
- queueing_lock wörtlich (Warning-Kasten auf howto/basics/batch_defer.html): "Don't batch defer multiple jobs where the task has the same configured queuing lock, because that would directly raise an AlreadyEnqueued exception and none of those jobs are deferred (the database transaction will be fully rolled back)."

**Konsequenz für den Plan:** Trägt wie geplant für das Massen-Einreihen von Dokument-Jobs — aber `batch_defer` darf nicht mit einem Task benutzt werden, der einen `queueing_lock` trägt, und ein Batch, in dem ein Konflikt steckt, schreibt GAR NICHTS (voller Rollback); der Producer muss diesen Fall behandeln (Batch aufteilen oder einzeln nach-deferren).

---

## 10. LISTEN/NOTIFY

**Behauptung:** Der Worker nutzt LISTEN/NOTIFY für neue Jobs und Abort-Requests (`listen_notify=True`) mit Poll-Fallback (`fetch_job_polling_interval`). Zusatzfrage: Gibt es ein eingebautes Ergebnis-Notify an Aufrufer („Job X ist fertig"), oder muss das die Anwendung selbst machen?

**Befund:** Hauptteil BESTÄTIGT; Zusatzfrage: ein eingebautes Fertig-Notify ist NICHT BELEGT — in der Doku existiert kein solches Feature, die Anwendung muss es selbst bauen.

**Beleg:**
- "listen_notify — If `True`, allocates a connection from the pool to listen for: - new job availability - job abort requests. Provides lower latency for job updates compared to polling alone. Note: Worker polls the database regardless of this setting. (defaults to `True`)" (reference.html, `App.run_worker_async`). Grundprinzip: "PostgreSQL's LISTEN allows us to be notified whenever a task is available." (discussions.html). Die NOTIFY-Verbindung ist eine eigene: "Procrastinate will use one connection to listen to server-side NOTIFY calls […] That connection is not counted in the pool" (discussions.html).
- Poll-Fallback: "Even when the database doesn't notify workers regarding newly deferred jobs, each worker still poll the database every now and then, just in case. There could be previously locked jobs that are now free, or scheduled jobs that have reached the ETA. fetch_job_polling_interval is the App.run_worker() parameter […] that sizes this 'every now and then'." (discussions.html; Default 5.0 s laut reference.html). Für Aborts analog `abort_job_polling_interval` (discussions.html, zitiert unter Punkt 3).
- Ergebnis-Notify: Die einzigen dokumentierten NOTIFY-Verwendungen sind die beiden oben (neue Jobs, Abort-Requests). Die einzige Listen-API im JobManager ist `listen_for_jobs(on_notification, queues=None)`, und die hört ausdrücklich auf DEFER-Operationen: "If an iterable of queue names is passed, only defer operations on those queues will be considered." (reference.html). Ein Ereignis „Job abgeschlossen" an externe Warter kommt in der Doku nirgends vor; auch die Monitoring-Seite bietet nur Healthcheck und Shell ("The administration web portal — Not yet, maybe someday.", howto/production/monitoring.html).

**Konsequenz für den Plan:** Worker-Latenz trägt wie geplant; für „Dokument fertig"-Signale an die Client-Fassade muss die Anwendung selbst sorgen (z. B. eigenes `NOTIFY` am Ende des Tasks beim Schreiben in die `documents`-Tabelle, oder Polling der `documents`-Tabelle) — Procrastinate liefert das nicht.

---

## 11. Nachlesbarkeit (delete_jobs, procrastinate_events, remove_old_jobs)

**Behauptung:** `delete_jobs="never"` als Worker-Option (Jobs bleiben in der DB), `procrastinate_events`-Tabelle mit Ereignistypen, eingebauter Aufräum-Task `remove_old_jobs`.

**Befund:** BESTÄTIGT.

**Beleg:**
- `delete_jobs`: "If `always`, the worker will automatically delete all jobs on completion. If `successful` the worker will only delete successful jobs. If `never`, the worker will keep the jobs in the database. (defaults to `never`)" (reference.html, `App.run_worker_async`). Der Default ist also bereits das gewünschte Behalten. CLI: `procrastinate worker --delete-jobs=always` (howto/production/delete_finished_jobs.html).
- `procrastinate_events`: "there's another table, procrastinate_events, which contains rows pointing to jobs in the procrastinate_jobs table, dates & times and events." Ereignistypen wörtlich definiert (howto/advanced/events.html):
  - `deferred` — "The job has been enqueued, will be executed by the workers later."
  - `started` — "The job was started by a worker."
  - `deferred_for_retry` — "The job failed, but according to the retry strategy, it should be retried"
  - `failed` — "The job failed, and will not be retried."
  - `succeeded` — "The job succeeded."
  - `cancelled` — "The job was waiting to be executed, but was ultimately placed to failed or succeeded, bypassing execution."
  - `scheduled` — "This is a special event. When the job is deferred, this is the date where it's expected to run."
  Die Seite trägt den Hinweis "This is quite early-stage, likely to get a better interface in the future". Zusätzlich nennt die Retry-Seite einen Event-Typ `retried` beim manuellen Retry (howto/advanced/retry.html), der in dieser Liste fehlt — Widerspruch innerhalb der Doku, siehe Punkt 7.
- `remove_old_jobs`: "procrastinate.builtin_tasks.remove_old_jobs — This task cleans your database by removing old jobs. Note that jobs and linked events will be irreversibly removed" mit Parametern `max_hours` (Pflicht; "Only jobs which were finished more than max_hours ago will be deleted"), `queue`, `remove_failed`, `remove_cancelled`, `remove_aborted` (Default: nur erfolgreiche) (reference.html). Dokumentiertes Einbau-Muster als periodischer Task mit `@app.periodic(cron="0 4 * * *")` und `queueing_lock="remove_old_jobs"` (howto/production/delete_finished_jobs.html). Programmatische Entsprechung: `JobManager.delete_old_jobs(nb_hours, queue=None, include_failed=False, include_cancelled=False, include_aborted=False)` (reference.html).

**Konsequenz für den Plan:** Trägt wie geplant — Jobs und Events bleiben per Default nachlesbar, das Aufräumen ist ein bewusst zu aktivierender periodischer Task.

---

## 12. Daemon-Lebenszeichen von außen

**Behauptung:** Es gibt eine dokumentierte Worker-/Heartbeat-Tabelle oder API, mit der ein ANDERER Prozess prüfen kann, ob gerade ein Worker lebt (Erwartung: abfragbare Tabelle — Name, Felder).

**Befund:** PRÄZISIERT. Die Heartbeat-Daten liegen in der Datenbank und sind dokumentiert abfragbar — aber nur über das Django-Modell `ProcrastinateWorker` (Felder `id`, `last_heartbeat`); einen SQL-Tabellennamen nennt die stable-Doku nirgends, und eine „liste lebende Worker"-Abfrage-API im JobManager gibt es nicht.

**Beleg:**
- Heartbeats in der DB: "Procrastinate workers update heartbeats at a regular interval (every 10 seconds by default). […] Regular worker shutdowns delete the worker's heartbeat from the database. Heartbeats of stalled worker are also pruned after a certain duration (30 seconds by default)" (howto/production/retry_stalled_jobs.html).
- Dokumentierte Abfragefläche: "Procrastinate exposes 3 of its internal tables as Django models. You can use them to query the state of your jobs." — importiert werden dabei allerdings VIER Modelle: `ProcrastinateJob`, `ProcrastinateEvent`, `ProcrastinatePeriodicDefer`, `ProcrastinateWorker` (Widerspruch „3" vs. vier Klassen innerhalb der Seite selbst). Referenz der Worker-Klasse: "class procrastinate.contrib.django.models.ProcrastinateWorker(id, last_heartbeat)" (howto/django/models.html). `ProcrastinateJob` trägt außerdem ein Feld `worker` (ebd., Signatur der Klasse), und `JobManager.list_jobs` filtert nach `worker_id` (reference.html).
- JobManager-Methoden rund um Worker existieren (`register_worker`, `unregister_worker`, `update_heartbeat`, `prune_stalled_workers` — Methodenindex in reference.html), sind aber Worker-Lebenszyklus, keine Lese-API für Dritte; ein `list_workers` existiert nicht (0 Treffer in reference.html und searchindex.js). Die inverse Frage ist abgedeckt: `get_stalled_jobs(seconds_since_heartbeat=…)` liefert Jobs GESTORBENER Worker (reference.html).
- Negativ-Befund Tabellenname: "procrastinate_workers" kommt in der gesamten stable-Doku nicht vor (searchindex.js: 0 Treffer; reference.html: 0 Treffer). Die Doku nennt als Tabellen nur `procrastinate_jobs` und `procrastinate_events` (howto/advanced/events.html, howto/production/monitoring.html).

**Konsequenz für den Plan:** Plan muss angepasst werden: Die Client-Fassade kann Lebenszeichen nicht auf eine dokumentierte SQL-Tabelle stützen. Dokumentierte Wege: (a) das Django-Modell `ProcrastinateWorker.last_heartbeat` (nur wenn Django im Spiel wäre — ist es im Plan nicht), oder (b) indirekt über `get_stalled_jobs` bzw. `list_jobs(status="doing")`. Der tatsächliche SQL-Tabellenname und seine Spalten müssen am Objekt festgestellt werden (Vorhersage mit hoher Sicherheit, abgeleitet aus dem Django-Modellnamen und dem Muster der übrigen Tabellennamen: eine Tabelle namens `procrastinate_workers` mit mindestens `id` und `last_heartbeat`; das ist eine Vorhersage, kein Befund — direkter Zugriff darauf wäre zudem undokumentierte, versionsabhängige Schnittstelle, vgl. die Warnung auf howto/django/models.html: "We'll do our best to ensure backwards compatibility, but we won't always be able to do so.").

---

## 13. Sync-Producer

**Behauptung:** Aus synchronem Code deferren (`SyncPsycopgConnector`, `task.defer(...)`), während der Worker async läuft — dokumentiert unterstützt? Zwei Apps/Connectors auf dieselbe DB, oder eine App mit beiden Zugängen?

**Befund:** BESTÄTIGT — beide Muster sind dokumentiert.

**Beleg:**
- Sync-Connector, defer-only: "By setting your App's connector to an instance of SyncPsycopgConnector (or any other synchronous connector), you will get 'classic' synchronous I/O. Note that in this case, the only thing you'll be able to do is defer tasks. Other operations trigger an error with a synchronous connector." (howto/advanced/sync_defer.html). Weitere sync-Connectors: `Psycopg2Connector`, `SQLAlchemyPsycopg2Connector` (ebd.). Connector-Übersicht: "Asynchronous connectors are required for running the worker and can be used in both async and sync contexts. Synchronous connectors can only be used in sync contexts and are primarily for deferring jobs." (howto/basics/connector.html, via Context7).
- Worker braucht async: "you will always need to set up an asynchronous connector for running the worker, or the procrastinate CLI." (discussions.html).
- Eine App mit beiden Zugängen: "Async connectors are able to summon their synchronous counterpart when needed (using BaseConnector.get_sync_connector). All sync operations in Procrastinate (so mainly deferring tasks synchronously) will request the synchronous connector from the async connector under the hood." Mit Caveat: sync-Connector VOR dem Öffnen der App anfordern; nach dem Öffnen funktioniert die Kompatibilitätsschicht nur innerhalb von `asgiref.sync.sync_to_async`-Kontexten, sonst "you will likely get a RuntimeError" (howto/advanced/sync_defer.html).
- Getrennte Programme auf derselben DB sind das Grundmuster der Startseite: App mit `SyncPsycopgConnector` deferrt, "Somewhere in your program, run a worker (actually, it's usually a different program than the one deferring jobs for execution)" (index.html).

**Konsequenz für den Plan:** Trägt wie geplant — der sync Producer (eigene App mit `SyncPsycopgConnector`, nur `defer`) und der async Daemon (eigene App mit `PsycopgConnector`) auf derselben Datenbank sind genau das dokumentierte Grundmuster; wichtig ist nur, dass beide Apps dieselben Task-NAMEN kennen bzw. die Producer-Seite die Tasks importiert oder per Name deferrt.

---

## 14. Geplante Jobs

**Behauptung:** `schedule_in`/`schedule_at` beim defer (Job in der Zukunft). Bestätigen; und: hält ein scheduled-`todo`-Job den `queueing_lock`?

**Befund:** BESTÄTIGT; die Zusatzfrage ist mit hoher Sicherheit „ja", als direkte Ableitung aus zwei dokumentierten Sätzen (nicht als einzelner wörtlicher Satz belegt).

**Beleg:**
- Feature: "If a job is configured with a date in the future, it will run at the first opportunity after that date." — `create_bug.configure(schedule_at=dt).defer(...)` bzw. `clean.configure(schedule_in={"hours": 1, "minutes": 30}).defer()` (howto/advanced/schedule.html). `Task.configure`: `schedule_at` (datetime) und `schedule_in` (timedelta-kwargs-dict) sind gegenseitig exklusiv (`ValueError`), (reference.html). Job-Attribut: "scheduled_at: Date and time after which the job is expected to run." (reference.html).
- queueing_lock-Ableitung: (1) "queueing_lock allows a single job in todo status." (howto/advanced/queueing_locks.html); (2) ein geplanter Job ist bis zu seiner Fälligkeit ein `todo`-Job — die Polling-Doku behandelt "scheduled jobs that have reached the ETA" als Jobs, die der Worker beim Fetch aufsammelt (discussions.html), und das Zustandsmodell kennt keinen eigenen scheduled-Zustand (discussions.html: todo/doing/succeeded/failed/cancelled/aborted). Also hält ein scheduled Job den queueing_lock. Die queueing_locks-Seite legt dieselbe Kombination sogar als Muster nahe: "You might also want to schedule jobs for such task in the near future, which can be effective to throttle the task." (howto/advanced/queueing_locks.html).

**Konsequenz für den Plan:** Kein Planbestandteil (Alternative wurde geprüft und verworfen); festgehalten: das Feature existiert, und ein geplanter, noch nicht fälliger Job blockiert weitere defers mit demselben `queueing_lock`.

---

## 15. Prioritäten

**Behauptung:** Job-Prioritäten beim defer. Wertebereich/Sortierung?

**Befund:** BESTÄTIGT.

**Beleg:** `Task.configure`: "priority (int) - Set the priority of the job as an integer. Jobs with higher priority are run first. Priority can be positive or negative. The default priority is 0." (reference.html); ebenso als Task-Default über den Decorator: "priority (int) – Default priority (an integer) of jobs that are deferred from this task." (reference.html, `task` decorator). Sortierung im Zusammenspiel mit Locks: "they are processed one at a time in a specific order: first by descending priority (higher priority first), and then by ascending creation time (older job first). […] A high-priority job cannot interrupt a currently running job." (howto/advanced/locks.html). Ein begrenzter Wertebereich ist nicht dokumentiert (int, positiv oder negativ).

**Konsequenz für den Plan:** Trägt wie geplant (z. B. Eil-Dokumente höher priorisieren); Priorität ordnet nur die Reihenfolge des Abholens, verdrängt aber nie einen laufenden Job.

---

## 16. Testing

**Behauptung:** `procrastinate.testing.InMemoryConnector` + `app.replace_connector` + `run_worker(wait=False)` als offizielles Test-Muster.

**Befund:** BESTÄTIGT.

**Beleg:** Die Seite "Test your code that uses Procrastinate" (howto/production/testing.html) zeigt genau dieses Muster als Fixture:
```python
from procrastinate import testing
from mypackage.procrastinate import my_app, my_task

@pytest.fixture
def app():
    in_memory = testing.InMemoryConnector()
    with my_app.replace_connector(in_memory) as app:
        yield app

def test_my_task(app):
    my_task.defer(...)
    jobs = app.connector.jobs
    assert len(jobs) == 1
    app.run_worker(wait=False)
    assert task_side_effect() == "done"
    app.connector.reset()
```
Referenz: "class procrastinate.testing.InMemoryConnector — An InMemoryConnector may be used for testing only. Tasks are not persisted and will be lost when the process ends." Attribute/Methoden: `jobs` ("Mapping of {<job id>: <Job database row as a dictionary>}"), `reset()` ("Removes anything the in-memory pseudo-database contains, to ensure test independence.") (reference.html). Für Worker-Läufe in Tests empfiehlt die Django-Test-Seite zusätzlich `wait=False`, `install_signal_handlers=False`, `listen_notify=False` (howto/django/tests.html).

**Konsequenz für den Plan:** Trägt wie geplant.

---

## 17. Job-Status-Abfrage

**Behauptung:** `job_manager.get_job_status(job_id)` (sync+async) und `list_jobs`/`list_tasks`-artige Abfragen für Status-Aggregation.

**Befund:** BESTÄTIGT.

**Beleg:**
- Beides vorhanden: Der `JobManager`-Methodenindex in reference.html führt `get_job_status()` UND `get_job_status_async()`; die async-Variante ist beschrieben als "Get the status of a job by id. Parameters: job_id (int) […] Returns: Status" (reference.html).
- Listen-Abfragen (jeweils sync + async, Methodenindex reference.html): `list_jobs`/`list_jobs_async` — "List all procrastinate jobs given query filters" mit Filtern `id`, `queue`, `task`, `status`, `lock`, `queueing_lock`, `worker_id`, Rückgabe `Iterable[Job]`; `list_tasks`/`list_tasks_async` — Rückgabe "a list of dictionaries representing tasks stats (`name`, `jobs_count`, `todo`, `doing`, `succeeded`, `failed`, `cancelled`, `aborted`)"; analog `list_queues`/`list_queues_async` und `list_locks`/`list_locks_async`; außerdem `list_jobs_to_abort_async` (reference.html).
- Dazu die Admin-Shell mit den Kommandos `list_jobs`, `list_queues`, `list_tasks`, `list_locks`, `retry`, `cancel`, auch nicht-interaktiv: `procrastinate shell list_jobs` (howto/production/monitoring.html, dort als "Experimental feature" markiert).

**Konsequenz für den Plan:** Trägt wie geplant — die Client-Fassade kann Job-Status per `get_job_status`/`list_jobs` lesen und Aggregation über `list_tasks`/`list_queues` machen.

---

## 18. `lock`-Freigabe: bei JEDEM Job-Ende, nicht nur bei Erfolg (Nachtrag 06.08.)

**Behauptung (Plan 5.1):** Der zusätzliche `lock` serialisiert Jobs gleichen Locks; ein nachgeschobenes Duplikat startet erst nach dem laufenden Job. Offen war: Wird der `lock` auch frei, wenn der laufende Job SCHEITERT — oder könnte ein gescheiterter Job die Kette dauerhaft sperren?

**Befund:** BESTÄTIGT — Freigabe bei jedem Ende, Erfolg wie Misserfolg.

**Beleg:** Wörtlich von der Locks-Seite (howto/advanced/locks.html, vollständig gelesen am 06.08.): "In both cases, the second task cannot run before the first one has ended (successfully or not)." Reihenfolge bei mehreren Wartenden: "descending priority (higher priority first)", dann "ascending creation time (older job first)"; "If any job with the same lock is running, all other jobs with that lock must wait. A high-priority job cannot jump ahead of a currently running job, regardless of the running job's priority." Warnhinweise der Seite: Ein Job mit `scheduled_at` in der Zukunft blockiert nachfolgende Jobs desselben Locks bis zu seiner Verarbeitung; und "if the oldest task of a lock is in a queue that no worker consumes, the other tasks are blocked."

**Grenzfall dazu:** Ein STALLED Job (Worker tot, Job hängt in `doing`) ist noch nicht „ended" — er hält den Lock, bis der stalled-Wächter ihn zurücksetzt (Punkt 6). Das ist mit dem Plan verträglich: Das Duplikat wartet genau so lange, wie das Dokument ohnehin nicht doppelt laufen darf.

**Konsequenz für den Plan:** Die 5.1-Serialisierung kann nicht deadlocken; nach einem terminal gescheiterten Job läuft das nachgereihte Duplikat an — und verarbeitet das Dokument neu, weil der Terminal-Guard `failed` nicht deckt (gewollt, Plan 4.2/5.1: das ist der eine weitere Versuch).

---

## 19. `retry_job_by_id`: akzeptierte Zustände, `attempts`-Zählung, `aborted` nicht retrybar (Nachtrag 06.08., Objekt-Befund)

**Behauptung (Plan 4.2/7):** Failed-Dokumente werden über Procrastinates eigenes Retry-Tooling erneut vorgelegt. Offen war: Welche Job-Zustände nimmt das Tooling an (auch `aborted`?), und zählt die Wiedervorlage `attempts` hoch?

**Befund:** Am Objekt geprüft (SQL-Schema und Quellcode der installierten 3.9.0): akzeptiert werden NUR `doing` und `failed`; jedes Zurücksetzen auf `todo` erhöht `attempts` um 1; `aborted` wird mit einem Fehler abgewiesen.

**Beleg (`sql/schema.sql`, Funktion `procrastinate_retry_job_v2`; Pfade im Quellen-Nachtrag oben):**
- `SELECT status, abort_requested FROM procrastinate_jobs WHERE id = job_id AND status IN ('doing', 'failed') FOR UPDATE` — nur diese beiden Zustände kommen in Betracht; jeder andere endet in `RAISE 'Job was not found or has an invalid status to retry (job id: %)'`. Ein `aborted`-Job ist mit diesem Tooling also NICHT wieder vorlegbar (deckt sich mit der Cancellation-Doku: "When a job is requested to abort and fails, it will not be retried.").
- Der Rücksetz-Zweig: `SET status = 'todo', attempts = attempts + 1, scheduled_at = retry_at`, dazu `priority`/`queue_name`/`lock` per `COALESCE` (nur wenn übergeben).
- Sonderfall: Ein `doing`-Job MIT `abort_requested` wird nicht zurückgesetzt, sondern auf `failed` gestellt.
- Routing: `manager.py` — `retry_job(job, …)` ruft intern `retry_job_by_id_async(…)`; `queries.sql` kennt genau EINEN Retry-Aufruf (`SELECT procrastinate_retry_job_v2(…)`). Der stalled-Wächter (`get_stalled_jobs()` → `retry_job()`) und der manuelle Retry laufen also durch DIESELBE Funktion — die `attempts`-Antwort gilt für beide. Damit ist die offene Teilfrage aus Punkt 6 beantwortet: Stalled-Wiederaufnahmen zehren am `max_attempts`-Budget.
- Fertiges Hand-Werkzeug: `shell.py` — `procrastinate shell` mit den Kommandos `retry JOB_ID` (ruft `retry_job_by_id_async` mit `retry_at=jetzt`), `cancel`, `list_jobs`, `list_queues`, `list_tasks`, `list_locks`.

**Konsequenz für den Plan:** (a) Der Terminal-Guard des Tasks darf `failed` nicht decken, sonst wäre jeder manuelle Retry ein Noop, der als Erfolg verbucht wird (Plan 5.1). (b) Ein manueller Retry eines erschöpften Jobs ist genau EIN weiterer Versuch — `attempts` läuft weiter, es gibt keine neue Versuchs-Runde; als Kommentar an die Retry-Strategie. (c) Für `aborted`-Jobs (Shutdown) gibt es KEIN Retry-Bordmittel; ihr Heilungsweg ist das erneute Einreihen (`ocr_start`), wie der Plan es vorsieht (5.3). (d) `procrastinate shell` ist das fertige Hand-Werkzeug für Einzelfälle; die `retry_failed`-Fassade ist reiner Komfort darüber.

---

## Planänderungs-Bedarf

Nur die Punkte, bei denen der Befund vom Plan abweicht:

1. **Punkt 1 (Zusatzfrage):** Zwei parallele Worker in EINEM Prozess (zwei `run_worker_async`-Koroutinen mit getrennten Queues und getrennter concurrency) sind nirgends dokumentiert — weder erlaubt noch verboten. Das dokumentierte Modell ist: mehrere Worker = mehrere Prozesse; Parallelität innerhalb eines Workers = ein gemeinsamer `concurrency`-Pool über alle seine Queues. Wenn der Plan die Nicht-Verhungerung der maintenance-Queue auf die Zwei-Koroutinen-Konstruktion stützt, muss das am Objekt getestet oder auf zwei Prozesse umgeplant werden.
2. **Punkt 4:** Die Idempotenz-EMPFEHLUNG steht nicht in der Doku (das Wort kommt in der gesamten stable-Doku nicht vor). Die Anforderung selbst bleibt, aber ihre Begründung im Plan muss auf die Mechanik (Retries + stalled-Wiederaufnahme) umgestellt werden statt auf ein Doku-Zitat.
3. **Punkt 5:** Der `queueing_lock` ist bereits frei, sobald der Job `doing` wird — nicht erst bei terminalen Zuständen. Wo der Plan „kein zweites defer, solange eines läuft" braucht, ist zusätzlich `lock` nötig (dokumentierte Kombination beider).
4. **Punkt 9:** `batch_defer` mit einem `queueing_lock`-Task ist ausdrücklich untersagt; bei Konflikt wird der GESAMTE Batch zurückgerollt. Producer-Logik muss diesen Fall behandeln, falls Dokument-Tasks je einen `queueing_lock` bekommen.
5. **Punkt 10:** Ein eingebautes „Job fertig"-Notify an externe Warter existiert nicht; die Anwendung muss es selbst bauen (eigenes `NOTIFY` aus dem Task oder Polling der `documents`-Tabelle).
6. **Punkt 12:** Es gibt keine dokumentierte SQL-Tabelle und keine JobManager-Lese-API für „lebende Worker". Die Client-Fassade braucht dafür einen anderen Weg (indirekt über `get_stalled_jobs`/`list_jobs`, oder undokumentierter Direktzugriff auf die Worker-Tabelle — Letzteres wäre versionsabhängig und müsste am Objekt verifiziert werden).
7. **Punkt 19 (Nachtrag 06.08.):** Der Terminal-Guard des Tasks darf `failed` nicht decken (sonst ist jeder manuelle Retry ein Noop); `aborted`-Jobs sind nicht per Tooling retrybar (Heilung über erneutes Einreihen); jede Wiedervorlage zählt `attempts` hoch. Alles drei am 06.08. in den Plan eingearbeitet (5.1, 4.2, 5.3, 10.2).

## NICHT BELEGT — am Objekt zu testen

- **Zwei `run_worker_async`-Koroutinen in einem Prozess** (Punkt 1): funktioniert es, und isolieren die concurrency-Pools sauber?
- **Idempotenz-Empfehlung in der Doku** (Punkt 4): existiert dort nicht; der Sachverhalt Mehrfachausführung ist über die Mechanik belegt.
- **`attempts`-Zähler bei `retry_job` auf stalled Jobs** (Punkt 6): war offen — am 06.08. am Objekt beantwortet (Punkt 19): ja, jedes Zurücksetzen auf `todo` erhöht `attempts` und zehrt am Budget. Bleibt nur als Bestätigungs-Assertion im Integrationstest (Plan 10.2 j).
- **Eingebautes Ergebnis-Notify** (Punkt 10): existiert nicht in der Doku; Eigenbau nötig.
- **SQL-Name und Spalten der Worker-Heartbeat-Tabelle** (Punkt 12): in der Doku nur als Django-Modell `ProcrastinateWorker(id, last_heartbeat)` sichtbar; Tabellenname und Direktzugriff am Objekt feststellen.
- **Mengen-Limit für `batch_defer`** (Punkt 9): kein Limit dokumentiert; ob es praktisch eines gibt (Transaktionsgröße), müsste ein Test mit realistischen Batch-Größen zeigen.

## Festgestellte Widersprüche innerhalb der Doku (nicht aufgelöst, nur benannt)

- Die Retry-Seite (howto/advanced/retry.html) nennt beim manuellen Retry ein Event vom Typ `retried`; die Events-Seite (howto/advanced/events.html) listet in ihrer Ereignistypen-Aufzählung keinen Typ `retried`. Welche Seite den tatsächlichen Datenbestand beschreibt, gibt die Doku nicht her.
- Die Django-Modelle-Seite (howto/django/models.html) sagt "Procrastinate exposes 3 of its internal tables as Django models", listet aber vier Modellklassen (`ProcrastinateJob`, `ProcrastinateEvent`, `ProcrastinatePeriodicDefer`, `ProcrastinateWorker`).
- Das Howto-Beispiel für Custom-Retry benutzt `RetryDecision(should_retry=False)`; die Referenz-Signatur von `RetryDecision` kennt keinen Parameter `should_retry`. Der in beiden Quellen übereinstimmend dokumentierte Weg, einen Retry zu verweigern, ist `return None` aus `get_retry_decision`.

---

## Nachtrag 25.10.2026: Durchgang fuer Procrastinate 3.10.0 (Ticket Mineroo#22)

Anlass: Das Paket geht von 3.9.0 auf 3.10.0 (`procrastinate>=3.10.0`), weil das Ticket die Anbindung umbaut (eine App je Prozess). CLAUDE.md verlangt vor jedem Upgrade den Durchgang durch diese Datei; er ist hier festgehalten.

**Vorgehen, am Objekt:** Das Wheel `procrastinate-3.9.0` wurde heruntergeladen (ohne es zu installieren) und Datei fuer Datei mit der installierten 3.10.0 verglichen (`diff -rq`, dann `diff -u` der abweichenden Module). Dazu der volle Testlauf (`-m "not live"`) gegen 3.10.0 mit dem UNVERAENDERTEN Code, vor dem Umbau: 340 bestanden, 4 uebersprungen (die vier fehlenden Aufnahmen aus Mineroo#2) — das Upgrade allein bricht nichts.

**Was sich zwischen 3.9.0 und 3.10.0 aendert** (Module mit Unterschieden, alle uebrigen sind gleich): `app.py`, `manager.py`, `retry.py`, `psycopg_connector.py`, `sync_psycopg_connector.py`, `worker.py`, `testing.py`, `utils.py`, `connector.py`, `middleware.py`, `shell.py`, `cli.py` und die `contrib`-Konnektoren (aiopg, django, psycopg2, sqlalchemy). Das Verzeichnis **`sql/` ist unveraendert** (Schema und alle Abfragen, darunter `procrastinate_retry_job_v2`): Eine Migration gibt es nicht.

| Punkt | Befund fuer 3.10.0 |
|---|---|
| **12** (Worker-Tabelle `procrastinate_workers`, Heartbeats) | UNVERAENDERT. `sql/schema.sql` ist byte-gleich, `register_worker`/`update_heartbeat`/`prune_stalled_workers` im Manager stehen nicht im Unterschied. `db.worker_heartbeats` und die Tests dazu laufen gegen 3.10.0 gruen (`test_toter_daemon_wird_erkannt`, `test_ohne_daemon_passiert_nichts_und_das_wird_gesagt`). |
| **19** (`retry_job_by_id`: nur `doing`/`failed`, `attempts + 1`, `aborted` abgewiesen) | UNVERAENDERT. `procrastinate_retry_job_v2` ist in `sql/schema.sql` byte-gleich; `retry_job_by_id` und `retry_job_by_id_async` im Manager stehen nicht im Unterschied (neu ist nur der Parameter `connection=` an `cancel_job_by_id` und `get_job_status`, den dieses Paket nicht benutzt). Belegt durch `test_retry_von_hand_verarbeitet_wirklich_neu`, `test_jede_wiedervorlage_erhoeht_den_versuchszaehler`, `test_abbruch_beim_herunterfahren_heilt_durch_neu_einreihen` und `test_retry_failed_ueberspringt_aborted`, jetzt ueber die synchrone App des Clients. |
| **8** (eigene Retry-Strategie) | GEAENDERT, zum Guten. `RetryValue` heisst jetzt `bool \| int \| BaseRetryStrategy` (3.9.0: `RetryStrategy`), `get_retry_strategy` gibt `BaseRetryStrategy` zurueck: Der Widerspruch zwischen Doku und Typangabe ist behoben. Die Unterdrueckung `# type: ignore[call-overload]` in `tasks.py` fiel mit dem Upgrade als „unbenutzt" auf und ist entfernt, samt ihrem Kommentar. |
| **13** (Sync-Producer) | VERTIEFT. Der Client haelt jetzt eine EIGENE App mit `SyncPsycopgConnector`. Neu in 3.10.0: `SyncPsycopgConnector.execute_query_one_with_connection` (und das async Gegenstueck). `list_jobs`, `list_tasks` und `retry_job_by_id` laufen mit dem synchronen Konnektor (am Quelltext gelesen und in `tests/test_client.py` ausgefuehrt): Die Aussage „ein synchroner Konnektor kann nur einreihen" ist **am Objekt widerlegt** und aus dem Docstring in `tasks.py` entfernt. Nicht kann er einen Worker tragen. `app.close()` schliesst einen uebergebenen Pool nicht (`_pool_externally_set`), deshalb schliesst der Client seinen Pool selbst. |
| **1** (zwei Worker-Koroutinen in einem Prozess) | WEITER nur am Objekt belegt (`test_wartungs_queue_verhungert_nicht`, gruen). `worker.py` aendert sich in 3.10.0 nur kosmetisch (Log-Texte, eine Lambda-Bindung); der Fix fuer den Haenger beim Herunterfahren steckt in `utils.cancel_and_capture_errors`: Es bricht die Nebentasks eines Workers (Heartbeat, Zeitplan, Listener) bis zu fuenfmal erneut ab, wenn ein Abbruch verloren ging. Den Abbruch des WORKERS selbst, den der Daemon schickt, betrifft das nicht. `test_daemon_faehrt_sauber_herunter` und `test_gestorbener_worker_beendet_den_daemon_mit_fehler` laufen gegen 3.10.0 gruen (letzterer erst nach der Aenderung am Test, siehe den letzten Punkt unter „Neue Befunde"). |
| 2, 3, 4, 5, 6, 7, 9, 10, 11, 14, 15, 16, 17, 18 | Von keinem der Unterschiede beruehrt: Sie stuetzen sich auf Doku und SQL, und das SQL ist gleich. `perform_import_paths` haelt sein Ergebnis jetzt je App statt je Klasse (`lru_cache`), was zu zwei Apps im selben Prozess passt (die Tests haben zwei). |

**Neue Befunde dieses Nachtrags (nicht aus dem Versionsvergleich, sondern aus dem Umbau):**

- **`connection=` und der `InMemoryConnector`.** `InMemoryConnector.execute_query_all_with_connection` nimmt die Verbindung entgegen und tut nichts damit. Dass das Einreihen wirklich auf der Verbindung des Clients laeuft, ist deshalb nur gegen echtes Postgres zu pruefen (`test_einreihen_holt_keine_verbindung_aus_dem_pool`); offline schreibt ein Test-Konnektor die Verbindung mit.
- **`remove_old_jobs` wird bei jeder weiteren App im Prozess umbenannt** (laut Ticket; dieses Paket benutzt den Task nicht, `delete_jobs="never"`). Der Daemon-Prozess baut deshalb genau eine App (`test_daemon_prozess_haelt_genau_eine_app`).
- **`check=` und Abbruch.** psycopg_pool 3.3.0 fuehrt `CancelledError` in `CLIENT_EXCEPTIONS` (`pool_async.py`) und verschluckt einen Abbruch, der einen Task WAEHREND der Verbindungspruefung trifft; der Task laeuft weiter. Gilt auch fuer Procrastinates eigenen Standard-Pool. Folge fuer den Daemon: siehe den Kommentar an `tasks.worker_context_resources`.

**Andere Projekte der Miniconda-Base (Vorbedingung des Tickets, Qualitaetssicherung 25.10.):** `pip show` zeigt dazu nichts (`Required-by` ist leer, es kennt nur Pakete, keine Projekte); gesucht wurde deshalb in den Projekten unter `~/CodingProjects` nach `procrastinate`. Gefunden: `AntiFoodpoisoning/google-reviews-scraper-pro` (und eine Kopie davon). Es verlangt `procrastinate>=3.0.0` (ohne Obergrenze), baut `procrastinate.App(connector=PsycopgConnector(...))` und leitet `procrastinate.worker.Worker` ab (`HardFailWorker`), wobei es die **private** Methode `_persist_job_status(job, status, retry_decision, context, job_result)` ueberschreibt. **Es laeuft aber nicht auf der Miniconda-Base:** Der Eigentuemer hat nachgesehen, dass es ueber Docker laeuft (`run_workers.sh` ruft `docker compose` auf, `build.sh` baut das Image, das Dockerfile installiert die Abhaengigkeiten in ein eigenes `python:3.13-slim`, `start_worker.sh` ist der Einstieg im Container). Das Upgrade der Miniconda-Base auf 3.10.0 beruehrt es deshalb nicht, und die Annahme des Tickets, die Base werde von keinem anderen Projekt mit Procrastinate benutzt, bleibt fuer die Base richtig. Mineroo bleibt auf 3.10.0. Zur Vollstaendigkeit: In der installierten 3.10.0 hat `Worker._persist_job_status` dieselben fuenf Parameter wie oben und wird mit denselben Schluesselwoertern aufgerufen (`worker.py`); das waere also auch bei einem Aufbau des Images mit 3.10.0 kein Bruch an dieser Stelle. Der Code des Projekts wurde nicht ausgefuehrt.
