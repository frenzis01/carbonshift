# Piano: CarbonShift come servizio REST dockerizzabile

## Obiettivo

Trasformare `carbonshift_rs` da simulatore/benchmark offline a servizio di rete
che:

1. riceve richieste di esecuzione via REST/HTTP;
2. risponde subito al chiamante indicando lo slot temporale assegnato;
3. quando arriva lo slot, invia il job a un esecutore esterno (`IP:PORT`)
   indicando l'orario/slot di esecuzione;
4. riceve da quell'esecutore il risultato via callback HTTP;
5. inoltra il risultato al chiamante originale via il suo `callback_url`.

La dockerizzazione vera e propria (Dockerfile, compose, ecc.) è rimandata a un
prompt successivo — qui ci si limita a rendere il binario `carbonshift-service`
pronto per esserci messo dentro un container (config via env vars, bind
`0.0.0.0`, nessuno stato su disco per default).

## Valutazione dell'architettura proposta

L'idea di fondo è **sensata**: è un pattern *admission control + reservation +
esecuzione asincrona* comune nei job scheduler carbon-aware. Punti da tenere
presente (alcuni già affrontati in questa prima iterazione, altri restano
lavoro futuro — vedi sezione "Limitazioni note"):

- **Orizzonte di pianificazione finito**: il motore DP (`engine::scheduler`,
  `engine::dp_solver`) ragiona su un numero finito di slot (`Config::total_slots`).
  Analizzando `dp_solver.rs` per la Fase 6 ho verificato che il limite non è
  solo concettuale: `DpSolver::solve_batch` e `DpSolver::greedy_fallback`
  allocano array (`base_counts`, `inc_counts`, oltre al vettore
  `carbon_forecast` stesso) di lunghezza `total_slots`, **ad ogni singola
  risoluzione di batch** (`with_carbon_forecast` impone perfino
  `forecast.len() == window_size`). Un vero *rolling window* richiederebbe
  riscrivere questi array come struttura relativa/sparsa invece che densa e
  ancorata allo slot assoluto 0 — una riscrittura non banale del solver DP
  centrale (coperto da >40 unit test di correttezza), rischiosa da fare senza
  un budget di test dedicato. Ho quindi scelto una mitigazione **operativa**
  invece che un riscrittura dell'algoritmo (Fase 6, dettagli sotto):
  `GET /ready` segnala l'esaurimento imminente dell'orizzonte così che un
  orchestratore (Kubernetes, docker-compose, ecc.) possa avviare un'istanza
  sostitutiva *prima* che si raggiunga il limite, mentre l'istanza corrente
  continua a servire le richieste in volo fino allo shutdown (SIGTERM/SIGINT
  gestiti in modo graceful). Una riscrittura completa a rolling-window resta
  lavoro futuro esplicitamente rimandato.
- **SSRF sui `callback_url`**: dato che il servizio esegue POST verso URL
  forniti dal chiamante, è un vettore SSRF classico. Ho aggiunto una guardia
  minima (`service::handlers::validate_callback_url`): rifiuta schemi diversi
  da http/https e IP letterali loopback/private/link-local, a meno di
  `CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1` (utile solo per test locali). Non è
  una difesa completa: un hostname pubblico può comunque risolvere a un IP
  interno (DNS rebinding). Per produzione: allowlist di domini fidati
  configurata dall'operatore, o firewall di rete in egress.
- **Nessuna persistenza**: tutto lo stato (`SharedState`, richieste tracciate)
  è in memoria. Un riavvio del servizio perde le richieste in volo. Accettabile
  per una prima versione/demo; da valutare in futuro (DB/redis) se serve
  sopravvivere a riavvii o girare in più repliche.
- **Retry limitato**: il dispatcher ora ritenta con backoff esponenziale
  (`EXECUTOR_RETRY_BASE_MS`, cap `EXECUTOR_RETRY_MAX_MS`) fino a
  `EXECUTOR_MAX_RETRIES` tentativi, poi marca la richiesta `Failed` invece di
  ritentare all'infinito (Fase 3, completata).
- **Un solo esecutore configurato globalmente** (`EXECUTOR_URL`): l'architettura
  richiesta parla di *un* server IP:PORT, quindi va bene così; se in futuro
  serviranno più esecutori (per bilanciamento/failover), andrà introdotta una
  selezione per richiesta o un registro di esecutori.
- **Autenticazione**: aggiunta in Fase 4 — `CARBONSHIFT_API_KEY` (header
  `X-API-Key`) protegge `/v1/requests*`, `CARBONSHIFT_EXECUTOR_TOKEN` (header
  `X-Executor-Token`) protegge `/v1/callback/{id}`, con due segreti separati
  così il chiamante e l'esecutore non condividono lo stesso credential. Se non
  impostate, gli endpoint restano aperti (comportamento di default per demo
  locali) e il servizio logga un warning all'avvio.

In sintesi: architettura valida, ma da considerare "MVP funzionante" più che
"pronta per produzione" — le limitazioni sopra sono i punti da chiudere prima
di un rollout reale.

## Riorganizzazione del codice sorgente (Fase 1 — ✅ completata)

```
rust/src/
  engine/            # core di scheduling, nessuna I/O oltre al logging CSV
    config.rs, types.rs, dp_solver.rs, shared_state.rs, scheduler.rs,
    metrics_logger.rs, online_swarm.rs, online_swarmerge.rs
  sim/               # solo simulazione/benchmark, mai usato dal servizio REST
    generator.rs (arrivi sintetici/replay scenario), scenario.rs
  service/           # nuovo: livello REST/HTTP
    models.rs   — DTO richieste/risposte (serde)
    state.rs    — AppState condiviso (SharedState + tracking richieste HTTP)
    handlers.rs — submit / poll status / callback esecutore
    dispatcher.rs — task di sfondo: invia i job all'esecutore quando lo slot arriva
    server.rs   — router axum
  bin/
    nshift/        — benchmark multi-N esistente (invariato)
    simulate/      — ex src/main.rs, simulatore standalone (invariato, solo spostato)
    service/       — NUOVO binario `carbonshift-service`
  lib.rs           — dichiara engine/sim/service e ri-esporta i moduli di
                     engine/sim alla radice del crate (`crate::config`, ecc.)
                     così il codice esistente non ha dovuto essere toccato.
```

Nessun modulo di `engine` o `sim` è stato modificato nella logica; sono stati
solo spostati di file. I 40 unit test + i binari `nshift`/`simulate` compilano
e passano invariati dopo lo spostamento.

## Contratto REST (Fase 2 — ✅ implementata, MVP)

- `POST /v1/requests`
  - body: `{ "deadline_seconds": f64, "callback_url": "http://...", "payload": <json opzionale> }`
  - risposta `200 OK` se il solver ha già assegnato uno slot entro
    `SUBMIT_WAIT_TIMEOUT_SECS` (default 5s):
    `{ "request_id", "status": "scheduled", "scheduled_slot", "eta_seconds", "flavour", "carbon_cost" }`
  - risposta `202 Accepted` con `"status": "pending"` se il solver non ha
    ancora processato il batch: il chiamante può fare polling su
    `GET /v1/requests/{id}` o aspettare la callback.
- `GET /v1/requests/{id}` — stato corrente (`pending|scheduled|dispatched|completed|failed`).
- `POST /v1/callback/{id}` — chiamata dall'esecutore con
  `{ "success": bool, "result": <json>, "error": "..." }`; il servizio
  inoltra il risultato al `callback_url` originale (fire-and-forget, non
  blocca la risposta all'esecutore).
- `GET /v1/stats` — conteggio delle richieste tracciate per stato
  (`pending/scheduled/dispatched/completed/failed`), per osservabilità.
- `GET /health` — liveness.

`POST/GET /v1/requests*` richiede `X-API-Key` se `CARBONSHIFT_API_KEY` è
impostata; `POST /v1/callback/{id}` richiede `X-Executor-Token` se
`CARBONSHIFT_EXECUTOR_TOKEN` è impostata (confronto a tempo costante,
`service::auth`). Entrambe opzionali (default: endpoint aperti).

Il **dispatcher** (`service::dispatcher::run`) gira come task `tokio` in
polling ogni 200ms: quando `current_slot >= scheduled_slot` di una richiesta
ancora `Scheduled`, la invia (POST JSON) a `EXECUTOR_URL` con
`callback_url = SELF_BASE_URL/v1/callback/{id}`. Se `EXECUTOR_URL` non è
impostata, gira in **dry-run**: logga cosa avrebbe inviato senza fare la POST
(esattamente il comportamento "per i test si può mandare a vuoto" richiesto).
In caso di fallimento (rete o risposta non-2xx) ritenta con backoff
esponenziale (`EXECUTOR_RETRY_BASE_MS` → `EXECUTOR_RETRY_MAX_MS`); dopo
`EXECUTOR_MAX_RETRIES` tentativi la richiesta passa a `Failed` invece di
ritentare all'infinito. Logga anche un warning una tantum quando `current_slot`
supera il 90% di `TOTAL_SLOTS` (vedi Fase 6).

Il clock a slot reali è quello già esistente in `engine::scheduler::main_loop`
con `skip_empty_slots=false, slot_speed_scale=1.0` (nessuna modifica al motore
per questo): avanza col wall-clock invece di saltare gli slot vuoti come nelle
simulazioni offline.

## Stato di avanzamento

- [x] Fase 1 — riorganizzazione codice in `engine/` `sim/` `service/`
- [x] Fase 2 — scheletro REST funzionante: submit, poll, callback, dispatcher
      dry-run, guardia SSRF di base, nuovo binario `carbonshift-service`
      (verificato con smoke test manuale end-to-end: submit → scheduled →
      callback ricevuta → inoltrata al chiamante)
- [x] Fase 3 — irrobustimento dispatcher: backoff esponenziale + tentativi
      massimi (`EXECUTOR_MAX_RETRIES`/`EXECUTOR_RETRY_BASE_MS`/
      `EXECUTOR_RETRY_MAX_MS`) → stato `Failed` dopo N tentativi; endpoint
      `GET /v1/stats` per osservabilità (conteggio richieste per stato);
      warning una tantum quando ci si avvicina all'esaurimento di
      `TOTAL_SLOTS`.
- [x] Fase 4 — autenticazione minima: `X-API-Key` su `/v1/requests*`
      (`CARBONSHIFT_API_KEY`), `X-Executor-Token` su `/v1/callback/{id}`
      (`CARBONSHIFT_EXECUTOR_TOKEN`), confronto a tempo costante
      (`service::auth`), entrambe opzionali con warning di avvio se assenti.
- [x] Fase 5 — test automatici del layer `service`
      (`rust/tests/service_api.rs`, 10 test via `tower::oneshot`: auth,
      validazione, 404, stats, end-to-end con scheduler reale) + unit test
      su backoff, SSRF guard e confronto a tempo costante.
- [x] Fase 6 — mitigazione dell'orizzonte finito (non un rolling-window vero,
      vedi motivazione sopra): `GET /v1/horizon` riporta slot corrente/totale
      e frazione usata; `GET /ready` risponde `503` oltre
      `HORIZON_READY_THRESHOLD` (default 90%) così un orchestratore smette di
      instradare nuovo traffico e può avviare un'istanza sostitutiva; shutdown
      graceful anche su `SIGTERM` (non solo `SIGINT`), essenziale per
      `docker stop`/Kubernetes. Una vera riscrittura rolling-window del DP
      solver resta lavoro futuro (causa identificata: array `O(total_slots)`
      allocati per batch in `dp_solver.rs`).
- [x] Fase 7 — Dockerizzazione: `Dockerfile` multi-stage (build con
      `rust:1-bookworm`, runtime `debian:bookworm-slim` non-root, `HEALTHCHECK`
      su `/health`), `.dockerignore`, `docker-compose.yml` con un esecutore di
      esempio (`docker/mock_executor.py`, stdlib Python) per i test end-to-end.
      Verificato con build + `docker compose up` reale: submit → scheduled →
      dispatch al mock executor → callback → stato `completed`.

Documentazione completa dell'architettura, elenco file per file e istruzioni
dettagliate sui test (con e senza esecutore reale): vedi
[ARCHITECTURE.md](ARCHITECTURE.md).

## Come riprendere

Ogni fase sopra è indipendente e puo' essere ripresa leggendo questo file +
`rust/src/service/*`. Build/test:

```sh
cd rust
cargo build --bins        # nshift, simulate, carbonshift-service
cargo test --lib          # 49 unit test del core (engine) + service
cargo test --test service_api   # 10 test HTTP del layer service
LISTEN_ADDR=127.0.0.1:8099 EXECUTOR_URL= cargo run --bin carbonshift-service
```

Variabili d'ambiente del servizio: vedi doc-comment in
`rust/src/bin/service/main.rs`.
