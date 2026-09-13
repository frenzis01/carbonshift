# CarbonShift Client

Client dell'architettura CarbonShift: invia richieste a **carbonshift**
(non direttamente all'esecutore), carbonshift le inoltra all'esecutore nello
slot che decide, e il risultato torna a questo client tramite callback.
Raccoglie metriche end-to-end (tempi di invio/risposta, richieste servite
dopo la deadline, qualità/confidence riportate dal modello, ecc.).

```
client (questo)  --POST /v1/requests-->  carbonshift  --POST /dispatch-->  executor
       ^                                       |                              |
       |                                  (sceglie flavour                    |
       |                                   e slot in base al                  |
       |                                   carbon budget)                     |
       +--------------- POST /callback (risultato) <---- POST /v1/callback/{id} --+
```

**Importante**: il client non sceglie il flavour (Accurate/Balanced/Fast) —
lo decide carbonshift in base alla sua ottimizzazione carbon-aware. Il
client sceglie solo il *task* (`text_generation`/`ner`/`question_answering`),
l'input, e la `deadline_seconds` entro cui vuole il risultato.

## Setup

```sh
cd client
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # o requirements-dev.txt per i test
```

Il pacchetto `datasets` (Hugging Face) serve solo se userai `source: "dataset"`
(vedi sotto); con `source: "synthetic"` (default) non serve scaricare nulla e
puoi testare subito tutta la pipeline.

**Se vuoi usare dataset HuggingFace veri**: nessun account/login richiesto
per quelli usati qui. Per scaricarli in anticipo:

```sh
python scripts/download_datasets.py
```

Scarica `wikitext-2-raw-v1` (~4MB, prompt per text_generation), `squad_v2`
(~40MB, domande/risposte per question_answering) e `tomaarsen/conll2003`
(NER, mirror Parquet dello stesso dataset).

## Avvio (ordine consigliato)

1. **Executor** (cartella `../executor`): `uvicorn app.main:app --port 9000`
2. **Carbonshift** (cartella `../carbonshift/rust`), puntato all'executor:
   ```sh
   EXECUTOR_URL=http://localhost:9000/dispatch \
   CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1 \
     ./target/release/carbonshift-service
   ```
   `CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1` è necessario perché in locale sia
   l'URL di callback del client sia quello di carbonshift verso l'executor
   puntano a `localhost` — la guardia SSRF di carbonshift li rifiuterebbe
   altrimenti (vedi `carbonshift/rust/PLAN_SERVICE.md`).
3. **Client** (questa cartella): `uvicorn app.main:app --port 8100`

## Uso

Innesca l'invio di un batch di richieste (background, non blocca la risposta HTTP):

```sh
curl -X POST localhost:8100/run/send-batch -H 'Content-Type: application/json' -d '{
  "task": "text_generation", "count": 5, "deadline_seconds": 30, "source": "synthetic"
}'
```

oppure con lo script CLI:

```sh
python scripts/send_batch.py --task text_generation --count 5 --deadline-seconds 30
```

Poi osserva lo stato:

```sh
curl localhost:8100/requests               # tutte le richieste tracciate
curl localhost:8100/requests/<request_id>   # dettaglio di una
curl localhost:8100/metrics/summary         # report aggregato
```

**Nota sui modelli scaricati**: se sul tuo executor hai scaricato solo i
modelli di `text_generation`, usa `--task text_generation` — per `ner`/
`question_answering` l'executor scaricherà i modelli al primo utilizzo (o
usa `executor/scripts/download_models.py` prima).

## Timeslot e deadline con data

Oltre a `deadline_seconds` (relativo, usato da `/run/send-batch`), il
nuovo endpoint `POST /run/send-plan` accetta **orari assoluti** (con data,
per evitare ambiguità) per `start_at` (quando la richiesta "nasce") e
`deadline_at` (entro quando deve finire): entrambi discretizzati in timeslot
di `slot_minutes` (default 30) — es. una `deadline_at` di `19:32` con slot da
30 minuti diventa "entro le 20:00" (fine del timeslot `19:30–20:00`).

```sh
curl -X POST localhost:8100/run/send-plan -H 'Content-Type: application/json' -d '{
  "requests": [
    {"task": "text_generation", "input": {"prompt": "Hello"},
     "start_at": "2026-08-26T19:00:00Z", "deadline_at": "2026-08-26T19:32:00Z"}
  ],
  "slot_minutes": 30,
  "mode": "realtime"
}'
```

Con `mode: "realtime"` il client aspetta davvero che arrivi ogni `start_at`.
Con `mode: "emulated"` no — vedi sotto.

## Emulazione a tempo fittizio

Per testare un piano che copre ore/giorni di traffico senza aspettare il
tempo reale, usa `mode: "emulated"`: il client raggruppa le richieste per
timeslot (in base a `start_at`), invia tutte quelle di un timeslot subito,
poi sincronizza l'avanzamento dell'orologio con **due** chiamate dirette
(non tramite carbonshift, che farebbe da tramite solo per il traffico dati
normale):

1. `POST <carbonshift>/v1/admin/advance-slot` — carbonshift fa il flush di
   eventuali richieste rimaste in coda per il timeslot e aspetta che il suo
   dispatcher le consegni all'executor prima di rispondere.
2. `POST <executor>/admin/advance-slot` — l'executor esegue tutto ciò che è
   ora dovuto e risponde solo a cose fatte.

Solo a quel punto il client invia le richieste del timeslot successivo.
Richiede carbonshift avviato con `MANUAL_CLOCK=1` (e idealmente
`SUBMIT_WAIT_TIMEOUT_SECS` basso, es. `0.3`, altrimenti ogni invio blocca
fino a 5s di default) e l'executor con `EXECUTOR_MANUAL_CLOCK=1` — vedi
`carbonshift/rust/PLAN_SERVICE.md` §"Emulazione a tempo fittizio" per il
razionale completo del protocollo.

```sh
python scripts/run_emulation.py --slots 4 --per-slot 3 --slot-minutes 30 \
  --task text_generation --executor-url http://localhost:9000
```

Genera un piano sintetico (o da dataset con `--source dataset`) su N
timeslot e lo invia; segui i risultati con `curl localhost:8100/requests` /
`/metrics/summary` — un piano di ore/giorni simulati completa in pochi
secondi reali.

`--seed` è **random a ogni run** se omesso (stampato a schermo, es.
`seed=123456789 (random)`, per poterlo rifissare in una run successiva se
serve riprodurre esattamente lo stesso piano); passa `--seed N` per fissarlo.
Prima di questa modifica il default era sempre `42`, per cui run diverse
producevano sempre le stesse identiche richieste — ora invece variano a ogni
esecuzione a meno che tu non fissi esplicitamente il seed. Con `source:
"synthetic"` (default) ricorda comunque che gli esempi sono scelti (con
`random.choice`) da un pool piccolo (5 varianti per task) — usa `--source
dataset` per varietà reale su centinaia/migliaia di esempi.

### Con Docker Compose

Dalla cartella radice del workspace (non questa): il servizio one-off
`emulation` esegue lo script dentro la rete Docker, già puntato a
`client`/`carbonshift`/`executor` per nome host — non serve altro che i flag
di contenuto (`--task`, `--slots`, ecc.).

```sh
cd ..
export MANUAL_CLOCK=1 SUBMIT_WAIT_TIMEOUT_SECS=0.3 DISPATCHER_POLL_INTERVAL_MS=20
docker compose down   # se lo stack era già su con altre variabili
docker compose up -d --build
docker compose run --rm emulation --slots 4 --per-slot 3 --slot-minutes 30 --task text_generation
```

## API del client

| Endpoint | Scopo |
|---|---|
| `POST /run/send-batch` | Avvia (in background) l'invio di `count` richieste a carbonshift per il `task` scelto, con `deadline_seconds` relativo. `source: "synthetic"` (default, nessun download) o `"dataset"` (HuggingFace). |
| `POST /run/send-plan` | Come sopra ma con `start_at`/`deadline_at` assoluti e raggruppamento per timeslot; supporta `mode: "realtime"` o `"emulated"` (vedi sopra). |
| `POST /callback` | Riceve da carbonshift il risultato finale (`CallerCallbackPayload`); non richiamarlo manualmente, è per carbonshift. |
| `GET /requests` | Elenco di tutte le richieste tracciate con il loro stato. |
| `GET /requests/{id}` | Dettaglio di una richiesta (id = quello assegnato da carbonshift). |
| `GET /metrics/summary` | Report JSON aggregato per `task/flavour` (dettagliato). |
| `GET /metrics/progress` | Riepilogo rapido "a colpo d'occhio" di tutto il test in corso (conteggi + medie principali) — pensato per essere interrogato ripetutamente con `curl` mentre un test è in esecuzione. |
| `GET /health` | Liveness. |

## Metriche raccolte

Per ogni richiesta (persistite anche su `data/metrics.jsonl` non appena
arriva il risultato/timeout):
- `ack_latency_seconds`: tempo fra l'invio e la risposta sincrona di carbonshift
  (**non** include il tempo di esecuzione lato executor).
- `execution_time_seconds`: tempo speso dal solo executor per eseguire il
  modello (dal risultato inoltrato da carbonshift), quando disponibile.
- `end_to_end_seconds`: tempo fra l'invio e la ricezione del callback finale
  (include sia l'attesa dello scheduling sia l'esecuzione).
- `late`: `true` se `end_to_end_seconds > deadline_seconds` richiesta.
- `carbon_cost`, `flavour`, `scheduled_slot`, `eta_seconds`: dalla risposta di carbonshift.
- `scheduled_at`: timestamp (ISO8601) di quando il DP solver di carbonshift ha
  committato l'assegnazione di questa richiesta — distinto da
  `callback_received_at` (quando arriva il risultato dall'executor). `null`
  se la richiesta non è mai stata schedulata. Se la risposta sincrona di
  submit era ancora `pending` (`scheduled_slot`/`flavour`/`carbon_cost`/
  `scheduled_at` tutti `null`), alla ricezione della callback il client fa
  un `GET /v1/requests/{id}` per rinfrescarli — un callback arriva solo dopo
  che la richiesta è stata davvero schedulata e dispacciata, quindi a quel
  punto sono sempre disponibili.
- `baseline_carbon_cost`: quanto costerebbe (in termini di carbonio) eseguire
  la stessa richiesta *subito*, con il flavour più accurato/costoso e senza
  alcuna ottimizzazione della carbon intensity — calcolato da carbonshift al
  momento della sottomissione, è la baseline rispetto a cui si misura il risparmio.
- `carbon_saving_pct`: `(baseline_carbon_cost - carbon_cost) / baseline_carbon_cost * 100`
  — quanto si risparmia grazie alla scelta di carbonshift (flavour + slot)
  rispetto a quella baseline **analitica** (stima, non misura reale).
- `baseline_execution_time_seconds`: tempo di esecuzione **misurato** di una
  passata "ombra" con il flavour Accurate sullo stesso input (eseguita
  dall'executor subito dopo quella assegnata, a meno di
  `EXECUTOR_COMPUTE_QUALITY_BASELINE=0` — vedi executor/README.md). A
  differenza di `baseline_carbon_cost`, questo è un dato **empirico**, utile
  per un risparmio energetico/tempo reale invece che stimato.
- `energy_saving_pct`: `(baseline_execution_time_seconds - execution_time_seconds) / baseline_execution_time_seconds * 100`
  — l'analogo empirico di `carbon_saving_pct`, basato su tempi di esecuzione
  realmente misurati anziché sulle durate assunte in `config.rs`.
- `confidence`, `quality_score`: dal risultato dell'executor. `confidence` è
  sempre popolato quando il modello può produrne uno (score nativo della
  pipeline per QA/NER; per `text_generation` un self-score
  `exp(mean log-prob)` calcolato dall'executor sui token generati).
  `quality_score` usa, in ordine di priorità: (1) un campo `reference*`
  fornito dal chiamante, se presente (vero ground truth); (2) altrimenti, un
  confronto fra l'output del flavour assegnato e quello della stessa passata
  "ombra" col flavour Accurate (proxy di ground truth, sempre disponibile a
  meno di `EXECUTOR_COMPUTE_QUALITY_BASELINE=0`). Resta `null` solo se
  entrambe non sono disponibili.
- Stato: `submitted` → `completed` | `failed` | `timed_out` (nessun callback entro `CLIENT_CALLBACK_TIMEOUT_SECONDS`, default 120s).

`GET /metrics/summary` aggrega tutto questo per coppia `task/flavour`: conteggi,
tasso di richieste in ritardo (`late_rate`), statistiche (min/avg/max) di
latenza, tempo di esecuzione (misurato e baseline), carbon cost/baseline/saving
(analitico), energy saving (empirico), confidence e quality_score. La stessa
aggregazione, ma su **tutte** le richieste indipendentemente dal task/flavour,
è disponibile in `overall` (utile per "quanto ho risparmiato in totale in
questo test?"). C'è anche `scheduler`, letto **in diretta da carbonshift** (non
derivato dalle richieste tracciate, quindi `null` se carbonshift non è
raggiungibile): `global_error_avg`/`global_error_count` sono la media/il
conteggio errore realmente accumulati dallo scheduler (unica media, **non**
per task, per design — vedi PLAN_SERVICE.md); `tasks.<task>.max_error_threshold`
è la soglia **dichiarata** effettivamente in vigore per quel task (override
registrato via `push_flavours.py`, o il default globale di carbonshift).

`GET /metrics/progress` dà invece una fotografia unica e leggera dell'intero
test (non per task/flavour): quante richieste sono state inviate, quante
schedulate da carbonshift, quante completate/fallite/scadute, e le medie di
quality_score, confidence, `execution_time_seconds`, `ack_latency_seconds`,
`carbon_saving_pct` e `energy_saving_pct`.

```sh
curl localhost:8100/metrics/progress
```

## Test strutturati (battery runner)

Per valutare le performance in modo ripetibile — sia con pochi "microtest"
(5-10 richieste, per verificare che l'architettura funzioni) sia con run più
estesi — c'è uno script dedicato in `tests/battery/`, che rispecchia nello
spirito `carbonshift/tests/battery/run_battery.py` (scenari config-driven,
cartella di output con timestamp, CSV + README) ma con output in una cartella
e formato diversi, viste la natura diversa del test (HTTP/async/callback
contro solver Rust puro):

```sh
python tests/battery/run_battery.py --config tests/battery/battery_config_micro.json   # 6 richieste, sanity check
python tests/battery/run_battery.py --config tests/battery/battery_config.json          # scenari misti, incl. uno da dataset HF
```

Ogni scenario del file di config specifica liberamente `task`, `count`
(numero di richieste), `per_slot`/`slot_minutes`, `source` (`synthetic` o
`dataset`, con relativo dataset HuggingFace scelto in `app/datasets.py`),
`seed` e `mode` (`realtime` o `emulated`). Il flavour non è impostabile
direttamente (lo sceglie carbonshift), ma i risultati sono comunque
osservabili per flavour tramite `/metrics/summary`.

Ogni run scrive in `tests/battery/results/<battery_id>_<timestamp>/`:
- `battery_config.json`: snapshot della config usata;
- `README.md`: tabella riassuntiva per scenario (completate/fallite/scadute,
  quality/confidence medi, tempo di esecuzione medio, carbon saving medio);
- `results.csv`: le stesse metriche in formato tabellare;
- una sottocartella per scenario con `config.json`, `requests.jsonl` (i
  record grezzi per singola richiesta) e `metrics.json`.

## Variabili d'ambiente

| Variabile | Default | Scopo |
|---|---|---|
| `CARBONSHIFT_URL` | `http://localhost:8080` | Dove sta carbonshift. |
| `CARBONSHIFT_API_KEY` | *(assente)* | Se carbonshift ha `CARBONSHIFT_API_KEY` impostata, va replicata qui. |
| `CLIENT_SELF_BASE_URL` | `http://localhost:8100` | URL con cui questo client si presenta a carbonshift per il callback. |
| `CLIENT_HOST` / `CLIENT_PORT` | `0.0.0.0` / `8100` | Bind HTTP (se avvii con `uvicorn --host/--port`, quelli hanno precedenza). |
| `CLIENT_HTTP_TIMEOUT_SECONDS` | `10` | Timeout della chiamata a carbonshift. |
| `CLIENT_CALLBACK_TIMEOUT_SECONDS` | `120` | Dopo quanto una richiesta senza callback viene marcata `timed_out`. |
| `CLIENT_METRICS_PATH` | `data/metrics.jsonl` | File di append delle metriche. |
| `CLIENT_ADMIN_TIMEOUT_SECONDS` | `60` | Timeout per le chiamate `/admin/advance-slot` (modalità emulazione) — più generoso di `CLIENT_HTTP_TIMEOUT_SECONDS` perché comportano un flush + un'inferenza reale. |
| `EXECUTOR_ADMIN_URL` | `http://localhost:9000` | Base URL dell'executor per `mode: "emulated"` (può essere sovrascritto per richiesta con `executor_url`). |

## Test

`tests/test_tracker.py` (unità, nessuna rete) e `tests/test_server.py`
(HTTP via `TestClient`, con `carbonshift_client.submit` sostituito da uno
stub): **non** serve carbonshift/executor in esecuzione né il pacchetto
`datasets`.

```sh
pip install -r requirements-dev.txt
pytest -q
```

## Docker

```sh
docker build -t carbonshift-client .
docker run --rm -p 8100:8100 -e CARBONSHIFT_URL=http://<host>:8080 carbonshift-client
```

Per l'intero stack (carbonshift + executor + client) usa il
`docker-compose.yml` nella cartella radice del workspace:

```sh
cd ..
docker compose up --build
```

## Flavour dinamici per task (dati freddi calibrati)

Carbonshift non conosce di per sé alcun task: i suoi 3 flavour
(Accurate/Balanced/Fast) di default sono un'unica lista globale con errori
stimati "a occhio". Per farlo pianificare in base a **errore/costo reali per
ciascun task**, invece:

1. **Calibrazione una tantum** (nell'ambiente dell'**executor**, ha bisogno di
   torch/transformers): esegue davvero ogni modello configurato e misura
   errore reale (da `quality_score`) e tempo di esecuzione medio.
   ```sh
   cd ../executor
   python scripts/calibrate_models.py --samples 20
   ```
   Scrive/aggiorna `client/model_stats.json`, un dizionario **indicizzato per
   nome di modello** (non per task/flavour): così puoi cambiare o aggiungere
   modelli in `executor/app/config.py` e ricalibrare solo quelli, senza
   perdere le misurazioni più vecchie di modelli non più configurati.
2. **Invio a carbonshift**: raggruppa `model_stats.json` per task e registra
   ciascun set di flavour via `POST /v1/tasks`.
   ```sh
   python scripts/push_flavours.py
   ```
   Da quel momento, le richieste inviate con quel `task` (il client lo passa
   già automaticamente come `task_id`, vedi `submit(..., task_id=task)` in
   `app/runner.py`/`app/plan_runner.py`) vengono pianificate tra i flavour
   *misurati*, non quelli di default — mentre l'errore medio globale/di
   finestra dello scheduler resta un'unica media aggregata su tutte le
   richieste, indipendentemente dal task (per design: riflette la salute
   complessiva dello scheduler, non quella di un singolo task).

   Registra anche una soglia di errore (`max_error_threshold`) specifica per
   il task, di default al 75% fra l'errore minimo e massimo tra i suoi
   flavour calibrati (`--threshold-position` per cambiarlo): serve perché il
   default globale di carbonshift (4%) è pensato per un caso generico e può
   essere molto più severo di quanto qualunque modello reale di un task
   riesca a raggiungere (es. text_generation calibrato spesso supera il
   4% anche per il flavour più accurato) — senza una soglia dedicata quel
   task sarebbe permanentemente infattibile e finirebbe sempre nel fallback
   più costoso. Non tocca il vincolo di errore *globale* (sempre il default
   di carbonshift, per design task-agnostico).
3. **Correzione con l'errore reale** (solo nell'emulazione/prodotto reale, mai
   nelle simulazioni offline che non passano da carbonshift): quando arriva
   il callback dell'executor con `result.quality_score`, carbonshift sostituisce
   l'errore *previsto* del flavour assegnato con `(1 − quality_score) × 100`
   nella media globale/di finestra — vedi `carbonshift/rust/PLAN_SERVICE.md`.

## Semplificazioni note

- Nessun retry sull'invio a carbonshift (se fallisce, quella richiesta viene
  solo loggata e saltata).
- `source: "dataset"` per NER usa `tomaarsen/conll2003`, un mirror Parquet
  senza script di caricamento (il dataset originale `conll2003` non è più
  caricabile con le versioni recenti di `datasets`).
- Nessuna persistenza della coda/stato oltre il file JSONL di metriche: un
  riavvio del client perde il tracking delle richieste in volo (i callback
  che arrivano nel frattempo per id sconosciuti vengono solo loggati).
