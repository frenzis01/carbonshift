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
lo decide carbonshift in base alla sua ottimizzazione carbon-aware. Il client
sceglie il *task kind* dell'executor (`text_generation`/`ner`/
`question_answering`), l'input, la deadline e, opzionalmente, il QoS profile.
Il profilo è identificato da un ID stabile e condivisibile: due client che
inviano lo stesso `qos_profile_id` condividono il relativo budget; se il campo
è omesso, Carbonshift seleziona il profilo predefinito per quel task kind.

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

Per selezionare un budget condiviso, aggiungi `qos_profile_id` senza
sostituire il task kind:

```json
{
  "task": "question_answering",
  "qos_profile_id": "qa-standard-v1",
  "count": 5,
  "deadline_seconds": 30
}
```

Il profilo deve essere già registrato su Carbonshift con `POST /v1/profiles`.
L'helper `register_qos_profile` nel client e `scripts/push_flavours.py`
registrano profili; l'ID e la definizione devono essere identici tra i client
che vogliono condividere il budget.

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
    {"task": "text_generation", "qos_profile_id": "text_generation-calibrated-v1",
     "input": {"prompt": "Hello"},
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
tempo reale, usa `mode: "emulated"`. Il client registra il piano; è il provider
che guida i tick in ordine verso client, Carbonshift ed executor.
Richiede carbonshift avviato con `MANUAL_CLOCK=1` (e idealmente
`SUBMIT_WAIT_TIMEOUT_SECS` basso, es. `0.3`, altrimenti ogni invio blocca
fino a 5s di default) e l'executor con `EXECUTOR_MANUAL_CLOCK=1` — vedi
`carbonshift/rust/PLAN_SERVICE.md` §"Emulazione a tempo fittizio" per il
razionale completo del protocollo.

```sh
python scripts/run_emulation.py --slots 4 --per-slot 3 --slot-minutes 30 \
  --task text_generation --executor-url http://localhost:9000
```

Per impostazione predefinita lo script registra il profilo calibrato con ID
stabile `<task>-calibrated-v1` e lo include in ogni richiesta del piano.
Se una nuova calibrazione cambia la definizione, usa
`--profile-version v2` per registrare un profilo nuovo invece di sovrascrivere
quello esistente.
Per fare inviare richieste a più client sotto lo stesso profilo, un client
può registrarlo e gli altri possono usare `--no-register
--qos-profile-id question_answering-calibrated-v1`. Per usare il profilo
predefinito del task kind, passa `--no-register`
senza `--qos-profile-id`: il campo viene omesso.

La capacity-tier ladder è globale e non appartiene al profilo. Se vuoi
impostarla esplicitamente per un'emulazione, aggiungi:

```sh
python scripts/run_emulation.py --slots 4 --per-slot 3 \
  --set-global-capacity-tiers
```

Questo ricostruisce la ladder usata prima della migrazione ai QoS profile
(`per-slot` a moltiplicatore 1.0, fino a `1.5 × per-slot` a moltiplicatore
1.5, poi overflow a 5.0) e la invia **una volta prima del piano**. È un
override globale: influenza le richieste di tutti i profili e di tutti i
client su quell'istanza Carbonshift. Senza il flag il client non cambia le
tiers configurate nel server. L'endpoint
`PUT /v1/admin/capacity-tiers` è protetto dalla API key e restituisce `204`
quando la ladder globale è stata sostituita. Un aggiornamento attende che i
batch attivi finiscano solve e commit; le assegnazioni e baseline già
memorizzate restano ai costi precedenti, mentre i calcoli successivi usano la
nuova ladder. Per questo è consigliato impostarla prima di inviare il piano.

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
| `POST /run/send-batch` | Avvia (in background) l'invio di `count` richieste per il task scelto; accetta `qos_profile_id` opzionale e `deadline_seconds` relativo. `source: "synthetic"` (default) o `"dataset"` (HuggingFace). |
| `POST /run/send-plan` | Piano con `start_at`/`deadline_at` assoluti e raggruppamento per timeslot; ogni richiesta può avere un `qos_profile_id` opzionale. |
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
- `qos_profile_id`: ID effettivamente risolto da Carbonshift, incluso il profilo
  predefinito quando il client non ne ha selezionato uno.
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
raggiungibile): `global_error_avg`/`global_error_count` sono telemetria
descrittiva aggregata, non una garanzia QoS tra semantiche diverse.
`profiles.<qos_profile_id>` espone task kind, semantica dell'errore, soglia,
finestra e vincolo cumulativo dei profili effettivamente usati.

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
| `CLIENT_QOS_PROFILE_VERSION` | `v1` | Versione stabile usata per generare gli ID dei profili calibrati; aumentala quando cambia la policy. |
| `CLIENT_QOS_PROFILE_THRESHOLD_POSITION` | `0.75` | Posizione fra errore minimo e massimo usata per la soglia dei profili calibrati. |
| `CLIENT_QOS_PROFILE_STATS_PATH` | `model_stats.json` nel servizio client | Snapshot di calibrazione da cui ricostruire i profili calibrati all'avvio. |
| `CLIENT_QOS_PROFILE_DEFINITIONS_PATH` | *(assente)* | Percorso opzionale a un array JSON di profili QoS completi da ripristinare all'avvio. |
| `CLIENT_QOS_PROFILE_REGISTRATION_ATTEMPTS` | `10` | Tentativi di registrazione iniziale per profilo, per gestire l'ordine di avvio dei servizi. |
| `CLIENT_QOS_PROFILE_REGISTRATION_RETRY_SECONDS` | `2` | Attesa fra tentativi transitori di registrazione profilo. |

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
2. **Registrazione su carbonshift**: raggruppa `model_stats.json` per task e
   registra un profilo versionato per ogni task kind.
   ```sh
   python scripts/push_flavours.py
   ```
   I profili usano ID stabili (`<task-kind>-calibrated-v1`), i flavour
   *misurati*, la soglia interpolata e la semantica d'errore salvata nei dati
   di calibrazione. La finestra e il vincolo cumulativo sono espliciti nella
   registrazione: due client che riutilizzano lo stesso ID devono quindi
   inviare la stessa definizione. Se una nuova calibrazione cambia una policy,
   usa `--profile-version v2` per creare un ID diverso. Le richieste selezionano il budget con
   `qos_profile_id`; il task kind continua a descrivere l'operazione
   dell'executor. `POST /v1/tasks` resta solo un adapter di compatibilità.
   Profili differenti non condividono il budget e vengono pianificati in
   batch separati. Le capacity tiers restano globali e contano le richieste
   di tutti i profili.

   La soglia è di default al 75% fra l'errore minimo e massimo dei flavour
   calibrati (`--threshold-position` per cambiarlo). La soglia e i vincoli
   cumulativi si applicano al QoS profile, non a una media hard aggregata tra
   task diversi. L'endpoint `/v1/stats` conserva una media fleet-wide solo
   come telemetria descrittiva.
3. **Correzione con l'errore reale** (solo nell'emulazione/prodotto reale, mai
   nelle simulazioni offline che non passano da carbonshift): quando arriva
   il callback dell'executor con `result.quality_score`, carbonshift sostituisce
   l'errore *previsto* del flavour assegnato con il valore riportato dal task
   nella finestra e nei totali del profilo associato — vedi
   `carbonshift/rust/PLAN_SERVICE.md`.

## Riavvii e migrazione da `task_id`

Carbonshift conserva i profili QoS personalizzati in memoria. All'avvio il
client ricostruisce i profili calibrati dal proprio `model_stats.json` e
registra anche gli eventuali profili completi configurati con
`CLIENT_QOS_PROFILE_DEFINITIONS_PATH`. Le registrazioni identiche sono
idempotenti; un ID già usato con una definizione diversa è un errore e non
viene sovrascritto. Il client riprova gli errori di connessione e server con
limiti configurabili; errori permanenti di definizione impediscono l'avvio.
Se cambia la calibrazione, aumenta la versione e mantieni allineati
`CLIENT_QOS_PROFILE_VERSION`, `push_flavours.py --profile-version` e
`run_emulation.py --profile-version`: i nuovi profili hanno un ID diverso e
possono essere ripristinati dalla stessa configurazione sui client.

Se Carbonshift viene riavviato mentre il client resta attivo, una richiesta
con un profilo noto al catalogo locale riceve un solo tentativo di
registrazione idempotente e viene reinviata una volta. Se il profilo non è
nel catalogo locale, l'invio fallisce esplicitamente: non viene sostituito
silenziosamente con un profilo predefinito.

Per un profilo personalizzato, configura il file in tutti i client che
devono poterlo ripristinare:

```json
[
  {
    "profile_id": "qa-standard-v2",
    "task_kind": "question_answering",
    "flavours": [{"name": "Accurate", "error": 4.0, "duration": 120}],
    "error_semantics": "word-overlap-f1-v1",
    "max_error_threshold": 10.0,
    "error_window": {"past_slots": 12, "future_slots": 14, "past_decay_slots": 12},
    "cumulative_error": {"enabled": true, "hard": true}
  }
]
```

`CLIENT_QOS_PROFILE_DEFINITIONS_PATH` deve puntare a questo file **dentro**
ogni container client. Un profilo personalizzato registrato soltanto da uno
script/altro client non può essere ricostruito da un processo che non possiede
la sua definizione.

Il ripristino riguarda le **definizioni** e gli ID stabili, non la persistenza
dello stato dello scheduler. Un riavvio Carbonshift azzera code, assegnazioni,
contatori di errore e cronologia in memoria; non reinterpreta però le
definizioni del profilo con un ID diverso. I contatori di compatibilità
`GET /v1/stats` (`legacy_task_id_usage`) sono anch'essi process-locali e si
azzerano al riavvio.

I client aggiornati inviano `task_kind` e, se scelto, `qos_profile_id`; non
inviano più `task_id`. Carbonshift mantiene ancora il campo legacy nelle
richieste, gli endpoint `/v1/tasks` e il filtro query `task_id`.
Prima di rimuoverli, verifica che i contatori `request_submissions`,
`task_api_calls` e `monitoring_queries` siano tutti zero per almeno 30 giorni
di esercizio dopo aver aggiornato ogni client mantenuto. Poiché i contatori
si azzerano a ogni riavvio, conserva la serie fuori da Carbonshift (ad esempio
nel monitoraggio operativo). La rimozione avverrà poi in una modifica
separata e dichiaratamente breaking; questo rilascio non rimuove gli adapter.

## Semplificazioni note

- Nessun retry sull'invio a carbonshift (se fallisce, quella richiesta viene
  solo loggata e saltata), salvo il singolo ripristino idempotente di un
  profilo noto quando Carbonshift segnala che il suo registro in memoria è
  stato azzerato.
- `source: "dataset"` per NER usa `tomaarsen/conll2003`, un mirror Parquet
  senza script di caricamento (il dataset originale `conll2003` non è più
  caricabile con le versioni recenti di `datasets`).
- Nessuna persistenza della coda/stato oltre il file JSONL di metriche: un
  riavvio del client perde il tracking delle richieste in volo (i callback
  che arrivano nel frattempo per id sconosciuti vengono solo loggati).
