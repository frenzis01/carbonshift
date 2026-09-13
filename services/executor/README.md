# CarbonShift Executor

Servizio esterno "esecutore" per l'architettura CarbonShift: riceve job HTTP
(tipo di task + flavour + orario di esecuzione), li mette in coda per
timeslot, esegue **un job alla volta** (nessun parallelismo) usando un
modello Hugging Face, e restituisce il risultato via callback HTTP.

Vive in una cartella separata (allo stesso livello di `carbonshift/`) perché
è un servizio indipendente: oggi testabile da solo via `curl`, in futuro
raggiungibile da `carbonshift` impostando `EXECUTOR_URL` (vedi
["Integrazione con carbonshift"](#integrazione-con-carbonshift)).

## Task e modelli

Tre tipi di task, tre "flavour" ciascuno (stessa idea di Accurate/Balanced/Fast
di carbonshift), scelti piccoli/veloci per girare su hardware comune (testato
per un i7-7700, 16GB RAM, GTX 1060 3GB):

| Task | Fast | Balanced | Accurate |
|---|---|---|---|
| `text_generation` | `distilgpt2` (~330MB) | `gpt2` (~500MB) | `gpt2-medium` (~1.4GB) |
| `ner` | `dslim/distilbert-NER` (~260MB) | `dslim/bert-base-NER` (~430MB) | `dslim/bert-large-NER` (~1.3GB) |
| `question_answering` | `distilbert-base-cased-distilled-squad` (~260MB) | `deepset/bert-base-uncased-squad2` (~430MB) | `deepset/bert-large-uncased-whole-word-masking-squad2` (~1.3GB) |

Dimensioni indicative su disco (pesi in fp32); su GPU occupano circa la
metà con inferenza in fp16. Con una GTX 1060 da 3GB **non tutti e 9 i modelli
stanno in VRAM insieme** — per questo l'executor tiene un piccolo LRU cache
(`EXECUTOR_MAX_LOADED_MODELS`, default 2 modelli caricati contemporaneamente)
e carica gli altri al volo quando servono, scaricando dalla cache il meno
usato di recente. Per l'uso su CPU questo non è un problema (solo più lento).

## Setup

```sh
cd executor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # o requirements-dev.txt per i test
```

**Se non hai mai usato Hugging Face**: non serve nessun account/login per
questi modelli (nessuno è "gated"). Al primo utilizzo di un `(task, flavour)`
i pesi vengono scaricati automaticamente in `~/.cache/huggingface` (o in
`$HF_HOME` se impostata). Se preferisci scaricarli tutti in anticipo (utile
prima di una demo, per non aspettare il download durante la prima richiesta):

```sh
python scripts/download_models.py
```

Per la GPU: `pip install -r requirements.txt` installa `torch` da PyPI, che
su Linux include già il supporto CUDA per GPU comuni (la GTX 1060, Pascal,
è supportata). Verifica con:

```sh
python -c "import torch; print(torch.cuda.is_available())"
```

Se stampa `False` ma hai un driver NVIDIA installato, segui le istruzioni per
la build CUDA corretta su https://pytorch.org/get-started/locally/ — in ogni
caso l'executor funziona anche su sola CPU (`EXECUTOR_DEVICE=cpu`), solo più
lentamente.

## Avvio

```sh
uvicorn app.main:app --host 0.0.0.0 --port 9000
```

Variabili d'ambiente (tutte opzionali):

| Variabile | Default | Scopo |
|---|---|---|
| `EXECUTOR_HOST` / `EXECUTOR_PORT` | `0.0.0.0` / `9000` | Bind HTTP (usati solo se avvii con `python -m app`; con `uvicorn` passa `--host`/`--port`). |
| `EXECUTOR_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda`. |
| `EXECUTOR_MAX_LOADED_MODELS` | `2` | Quante pipeline tenere in cache (RAM/VRAM) contemporaneamente. |
| `EXECUTOR_POLL_INTERVAL_SECONDS` | `0.5` | Frequenza di controllo della coda quando è vuota/non ancora dovuta. |
| `EXECUTOR_METRICS_PATH` | `data/metrics.jsonl` | File JSONL di append delle metriche. |
| `EXECUTOR_CALLBACK_TIMEOUT_SECONDS` | `10` | Timeout HTTP per l'invio del callback. |
| `EXECUTOR_MANUAL_CLOCK` | `0` | Congela l'orologio della coda eccetto via `POST /admin/advance-slot` (solo test/emulazione). |
| `EXECUTOR_SLOT_MINUTES` | `30` | Durata di un timeslot in modalit\u00e0 manual clock (dovrebbe combaciare con `SLOT_DURATION_SECONDS` di carbonshift). |
| `EXECUTOR_COMPUTE_QUALITY_BASELINE` | `1` | Se attivo, ogni richiesta con flavour diverso da `accurate` esegue anche una passata "ombra" col flavour Accurate sullo stesso input, per ottenere `baseline_execution_time_seconds`/`baseline_model` (misura reale, non stimata) e un `quality_score` per confronto quando non è fornito un `reference*`. Circa raddoppia il calcolo per richiesta — disattivalo (`0`) nei test su larga scala dove non serve il confronto di qualità/tempo. |

## API

### `POST /jobs` — invio nativo (per test standalone, senza carbonshift)

```json
{
  "task": "question_answering",
  "flavour": "fast",
  "input": {"question": "Where is the Eiffel Tower?", "context": "The Eiffel Tower is in Paris, France."},
  "execute_at": "2026-08-26T18:00:00Z",
  "callback_url": "http://localhost:8123/my-callback"
}
```
`execute_at` è opzionale (default: subito). `callback_url` è opzionale: se
assente, il risultato va recuperato con `GET /jobs/{request_id}`.

Risposta `202`: `{"request_id", "status": "queued", "execute_at", "queue_position"}`.

Formato di `input` per task:
- `text_generation`: `{"prompt": str, "max_new_tokens"?: int, "reference"?: str}`
- `ner`: `{"text": str, "reference_entities"?: [{"text": str, "label": str}, ...]}`
- `question_answering`: `{"question": str, "context": str, "reference_answer"?: str}`

I campi `reference*` sono opzionali: se presenti, l'executor calcola anche un
`quality_score` (F1 su overlap di token) confrontando l'output con il valore
atteso — utile quando il client (prossimo step) userà dataset HuggingFace con
risposte note.

### `POST /dispatch` — adapter per carbonshift

Stesso schema esatto del payload che il dispatcher Rust di carbonshift invia
(`ExecutorDispatchPayload`, vedi `carbonshift/rust/src/service/models.rs`):
`{request_id, scheduled_slot, flavour, carbon_cost, callback_url, payload}`.
L'executor legge `task`/`input` (ed eventualmente `execute_at`) da dentro
`payload` — vedi [Integrazione con carbonshift](#integrazione-con-carbonshift).

### Altri endpoint

- `GET /jobs/{request_id}` — stato/risultato di un job.
- `GET /queue` — snapshot della coda, raggruppata per timeslot.
- `GET /models` — registro task → flavour → modello configurato.
- `GET /metrics/summary` — report JSON aggregato (count, tempo di esecuzione,
  confidence, quality_score, per ogni coppia task/flavour).
- `GET /metrics/raw?limit=100` — ultimi record grezzi.
- `GET /health` — liveness.

Ogni job eseguito viene anche accodato in append su `data/metrics.jsonl`
(percorso configurabile), indipendentemente dalle richieste HTTP al report.

## Esempi curl

```sh
# Text generation
curl -X POST localhost:9000/jobs -H 'Content-Type: application/json' -d '{
  "task": "text_generation", "flavour": "fast",
  "input": {"prompt": "The future of renewable energy is", "max_new_tokens": 30}
}'

# NER
curl -X POST localhost:9000/jobs -H 'Content-Type: application/json' -d '{
  "task": "ner", "flavour": "balanced",
  "input": {"text": "Barack Obama was born in Hawaii and became President of the United States."}
}'

# Question answering
curl -X POST localhost:9000/jobs -H 'Content-Type: application/json' -d '{
  "task": "question_answering", "flavour": "accurate",
  "input": {"question": "Where was Barack Obama born?",
            "context": "Barack Obama was born in Hawaii and became President of the United States."}
}'

# Risultato (sostituisci <id> con il request_id ricevuto)
curl localhost:9000/jobs/<id>

# Report metriche
curl localhost:9000/metrics/summary
```

## Test

Il test suite (`tests/test_api.py`) monkeypatcha l'esecuzione reale del
modello (`run_task`) con uno stub veloce: verifica routing HTTP, code per
timeslot, adapter `/dispatch`, metriche — **senza** scaricare/eseguire alcun
modello, quindi non serve `torch`/`transformers` installati né una GPU:

```sh
pip install -r requirements-dev.txt
pytest -q
```

### Test manuale con modelli reali

Dopo aver installato anche `torch`/`transformers` (`requirements.txt`
completo) e avviato `uvicorn app.main:app`, usa gli esempi curl sopra: la
prima chiamata per un dato `(task, flavour)` scarica il modello (se non già
scaricato con `scripts/download_models.py`), le successive sono immediate.

### Calibrazione modelli (errore/costo reali per carbonshift)

```sh
pip install datasets  # o requirements-dev.txt, che la include già
python scripts/calibrate_models.py --source dataset --samples 30
```

Esegue davvero ogni `(task, flavour)` configurato in `app/config.py` su
esempi reali (`--source dataset`, consigliata: centinaia/migliaia di esempi
con ground truth, invece dei ~5 esempi sintetici per task che possono far
oscillare la media su un campione piccolo), misura l'errore reale (da
`quality_score`) e il tempo di esecuzione medio, e aggiorna
`../client/model_stats.json` (indicizzato per nome di modello, così
cambiare/aggiungere modelli non perde le misurazioni più vecchie). Da lì,
`client/scripts/push_flavours.py` registra questi dati su carbonshift come
flavour dinamici per task — vedi `client/README.md` §"Flavour dinamici per task".

## Emulazione a tempo fittizio

Per test multi-timeslot senza aspettare il tempo reale (vedi
`carbonshift/rust/PLAN_SERVICE.md` §"Emulazione a tempo fittizio" per il
protocollo completo insieme a carbonshift), imposta `EXECUTOR_MANUAL_CLOCK=1`:
l'orologio della coda si congela e avanza solo tramite
`POST /admin/advance-slot`, che sposta il clock al timeslot successivo,
esegue (bloccando la risposta HTTP) tutti i job ora dovuti, e restituisce lo
stato finale della coda. Normalmente orchestrato dal client
(`client/scripts/run_emulation.py`), non va chiamato manualmente se non per
debug:

```sh
EXECUTOR_MANUAL_CLOCK=1 EXECUTOR_SLOT_MINUTES=30 uvicorn app.main:app --port 9000
curl -X POST localhost:9000/admin/advance-slot
```

## Integrazione con carbonshift e con il client

Il client (cartella `../client/`, vedi il suo README) invia le richieste a
carbonshift, non direttamente qui — questo executor riceve solo le
dispatch di carbonshift su `/dispatch`. Per collegarli:

```sh
EXECUTOR_URL=http://<host-executor>:9000/dispatch \
  ./carbonshift/rust/target/release/carbonshift-service
```

Il `payload` (campo opaco che carbonshift inoltra invariato) deve contenere
`{"task": ..., "input": {...}}` nel formato atteso da questo executor —
carbonshift si occupa solo di slot/flavour/tempistica, il contenuto del job
è tutto dentro `payload`.

## Docker

```sh
docker build -t carbonshift-executor .
docker run --rm -p 9000:9000 -v hf-cache:/root/.cache/huggingface carbonshift-executor
```

Il volume `hf-cache` evita di riscaricare i modelli ad ogni riavvio del
container. Per l'intero stack (carbonshift + executor + client) vedi il
`docker-compose.yml` nella cartella radice del workspace. L'immagine è
CPU-only per semplicità; per la GPU serve un'immagine base `nvidia/cuda` +
`--gpus all` (non incluso qui, per restare semplici — vedi i commenti nel
`Dockerfile`).

## Semplificazioni note

- Nessun retry sul callback (un solo tentativo, poi solo log) — coerente con
  "keep it simple"; se serve robustezza maggiore si può aggiungere lo stesso
  schema di backoff usato da carbonshift.
- Nessuna persistenza della coda: un riavvio perde i job non ancora eseguiti
  (le metriche già scritte su disco restano).
- `quality_score` è un semplice F1 su overlap di token, non una metrica NLP
  "vera" (BLEU/ROUGE/exact-match SQuAD) — sufficiente per un confronto
  relativo fra flavour, non per un paper.
- L'immagine Docker non include supporto GPU per default (vedi sezione Docker).
