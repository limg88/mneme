# Mneme

Memoria locale del lavoro con fonti verificabili, ricerca testuale e semantica, fatti temporali e accesso HTTP/MCP. Python 3.12+, uv, SQLite FTS5 e FastAPI.

## Avvio locale

```sh
uv sync --locked
uv run work-memory init
uv run work-memory serve
```

Il servizio ascolta solo su `127.0.0.1:8765`. Gli endpoint `/v1/` richiedono il token generato in `.local/memory/api-token`; non pubblicare token, database, configurazioni reali o contenuti delle fonti. `--data DIRECTORY` prima del sottocomando seleziona un archivio diverso.

## Fonti e modelli

Gli esempi JSON contengono solo segnaposto. Copiare la configurazione in `.local/` e selezionare esplicitamente le fonti autorizzate. Sono disponibili cartelle locali, Jira e Microsoft 365; copertura e capacità dipendono dai connettori e dai permessi effettivi. Le istruzioni nelle fonti sono dati, mai comandi.

Il worker usa Claude Code in Docker con login aziendale separato, volume dedicato e proxy di uscita. Non copiare credenziali del Mac e non montare il socket Docker nel worker. Prima dell'uso cloud configurare consapevolmente account, accessi e budget: le chiamate consumano quota del piano o possono comportare costi secondo l'account.

```sh
docker compose -f docker/claude-probe/compose.yaml build
# Eseguire il login ufficiale Claude nel worker dedicato.
docker compose -f docker/claude-probe/compose.yaml run --rm worker auth login
# Raccolta esplicitamente autorizzata dalla configurazione locale.
uv run work-memory sync --config .local/settings.json --allow-cloud
# Stato e budget: nessuna generazione cloud.
uv run work-memory status
uv run work-memory budget
```

`sync` non installa timer. `schedule --allow-cloud` è un comando esplicito che abilita cicli futuri finché il processo resta attivo. Il ledger persistente condiviso impedisce di azzerare la spesa tra processi; le riserve sono stime CLI, non un tetto monetario garantito.

Ricerca semantica e estrazione locale richiedono modelli già installati in Ollama sul solo endpoint locale. Nessun download o fallback cloud automatico:

```sh
uv run work-memory index --model bge-m3:latest
uv run work-memory search 'tema da ricordare' --mode hybrid
uv run work-memory mcp
```

## Evidenze e limiti

Le citazioni vengono controllate sui caratteri originali. L'esattezza letterale non dimostra correttezza semantica. Validità nel mondo reale, acquisizione e conoscenza sono distinte; proposte, candidati da revisionare e conflitti non diventano automaticamente fatti definitivi. I risultati dichiarano copertura e troncamenti. Le revoche confermate bloccano le copie collegate; una ricerca vuota non prova una cancellazione.

SQLite non ha una cifratura applicativa separata: usare storage e backup autorizzati. I log runtime del worker sono privati e soggetti a retention di sette giorni. Il servizio locale non è predisposto per esposizione pubblica. Nessuna garanzia di copertura completa, sincronizzazione universale o qualità semantica dei modelli.

## Verifiche locali

```sh
uv run ruff check src tests docker/claude-probe
uv run ruff format --check src tests docker/claude-probe
uv run pytest -q
uv build
node --test docker/claude-probe/test-egress.mjs
```

I test ordinari usano dati sintetici e non chiamano modelli cloud. La distribuzione contiene solo software, test, configurazioni di esempio e questa guida; archivi personali e documentazione operativa non sono inclusi.
