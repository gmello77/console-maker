# TODO console-maker

Stato al 2026-11-20. Le voci sono in ordine di lavoro. Convenzioni: `[x]` fatto e testato, `[~]` fatto ma con qualcosa da verificare, `[ ]` da fare.

## Fatto
- [x] v1: schermata Textual, ultima immagine Batocera dal mirror, download con ripresa, md5, ROM da `roms.yaml`, scrittura nella SHARE via Docker. Provata con una pipeline reale per l'RG SP (immagine da 9,1 GB, ROM verificata con SHA-256).
- [x] Punto 1: `item:` di archive.org con `include`/`exclude`/`files`, finestra "Scegli i giochi" (ricerca, file multi-disco raggruppati), scelta salvata in `selections.yaml`. Provato su archive.org vero solo per elenco file e URL.
- [x] Punto 2: adattamento automatico al disco (`priority` per sistema, opzione attiva di default, dimensione esatta letta dall'MBR dell'immagine compressa).
- [x] Punto 3, BIOS: sezione `bios:` in `roms.yaml` (`urls`/`item`, `dir`, `for`), in `userdata/bios`; contati nello spazio, mai sfoltiti, esclusi se i loro sistemi non ci sono. Provato con Docker vero. Non provato: BIOS reali su un dispositivo.
- [x] **Download a pezzi in parallelo** (`cm/download.py`): 4 connessioni di default (`connections` in `config.yaml`), ripresa per pezzo, fallback automatico se il server non accetta Range. Misurato su archive.org: ~7 volte più veloce di una connessione.
- [x] **Preconfigurazione** (`device.yaml`, `cm/device.py`): lingua, tastiera, fuso, hostname, Wi-Fi (più reti, una nascosta), ScreenScraper, chiavi extra. Fusa nel `batocera.conf` predefinito estratto dall'immagine (`builder/extract_defaults.sh`), verificato su quella vera: cambiano solo le righe scelte. Provato con Docker vero su immagine finta. **Non provato su un dispositivo**: che al primo avvio Batocera rispetti i file è dedotto dal codice di `S12populateshare`, non da un avvio.
- [x] Corretto: ultime opzioni non raggiungibili dopo il caricamento dei sistemi (colonne alte quanto il contenuto, clippate dal menu). Ora le colonne scorrono; test a 3 dimensioni di terminale con clic reale sull'ultima opzione.
- [x] Due barre di avanzamento (file in corso e totale ROM+BIOS) con velocità e tempo stimato.
- [x] Download: stop immediato su 401/403/404/410 con messaggio chiaro, avviso se un item è ad accesso riservato.

## In corso
- [~] **Login archive.org** (per gli item riservati, es. `htgdb-gamepacks`, che dà 401 senza login).
  - Fatto e testato (59 test): `cm/auth.py` (sessione salvata con permessi 600, cookie solo per archive.org e sottodomini in HTTPS, password mai salvata, mai nei log), `cm/login_screen.py`, pulsante e stato nella schermata, README.
  - Bug risolto: l'id `login` era uguale nel pulsante principale e nella finestra, quindi il clic su "Accedi" apriva una seconda finestra. Ora gli id sono distinti e le finestre fermano la propagazione dei clic.
  - Corretto il 2026-11-20: il login usava `op=authenticate` (errore mio, scritto a memoria); il client ufficiale `ia` usa `op=login`. I messaggi d'errore ora dicono solo il motivo che archive.org dichiara (`account_not_found`, `account_bad_password`), un 401 generico non è più "password errata".
  - **Non verificato con un account vero:** il login riuscito (il formato della risposta è quello letto nel codice di `ia`, provato solo con credenziali false) e che i cookie arrivino al server dei dati dopo il redirect. Serve un account vero: provare con `htgdb-gamepacks`. Se un account gratuito non basta, il 401 col login ora dà un messaggio dedicato.

## Prossimi punti (priorità alta)
- [ ] **Verifica checksum delle ROM** con gli md5 di archive.org (già letti in `ArchiveFile.md5`, oggi non usati): ricontrollo dopo il download e riscarico se non corrispondono. File: `cm/download.py`.
- [ ] **Controllo spazio sul Mac** prima di partire (cache + stage + immagine, 9 GB e più).

## Richieste dell'utente ancora aperte
- [ ] **Filtro per genere** (azione, avventura...). Serve prima una fonte dati: i nomi file non contengono il genere. Candidate: DAT di libretro-database, metadati di ScreenScraper (account e limiti), gamelist. Da scegliere e verificare sul campo prima di promettere la funzione.
- [ ] **Recalbox**: estrarre un'interfaccia `Distro` astratta da `cm/distro.py`, poi verificare mirror, nomi immagine, layout delle partizioni (dove sta la partizione dati e di che tipo) e supporto reale all'RG SP. Se non supporta l'RG SP, valido solo per Raspberry Pi e PC.

## Qualità e robustezza
- [ ] Scelta del canale e della versione (stable o release candidate; oggi l'RG SP usa la RC senza chiedere).
- [ ] Immagine finale compressa (`.img.xz` o `.zip`, che Etcher legge).
- [ ] Scaricare più file contemporaneamente (oggi i file vanno in serie, ognuno con 4 connessioni; velocità ed ETA nella schermata sono già fatti).
- [ ] Controllo di integrità dei pezzi scaricati in parallelo (oggi si fida del server; il md5 finale di archive.org non è ancora verificato per le ROM).
- [ ] Aggiungere ROM a un'immagine o a una SD già pronta, senza rifare tutto.
- [ ] Filtri per regione (EU/USA/JP) e per demo/beta/proto.
- [ ] Priorità per singolo gioco, non solo per sistema (oggi dentro un sistema vale l'ordine alfabetico).

## Estensioni
- [ ] Altri modelli Anbernic (RG35XX SP, RG34XX SP, RG40XX...): una riga ciascuno in `TARGETS`; le varianti con pannello v2 e RAM ddr3 richiedono una guida nel menù.
- [ ] Scraping offline sul PC (Skyscraper), come alternativa allo scraper integrato.
- [ ] GitHub Actions con test unitari e UI (gli e2e richiedono Docker privilegiato).
- [ ] Supporto Windows (Docker Desktop + WSL).

## Da verificare / chiudere
- [ ] Avviare un'immagine con `device.yaml` su un RG SP: verificare che lingua, fuso e Wi-Fi siano applicati al primo avvio e che lo scraper trovi le credenziali. Verificare anche l'escape di `# ; $` nella chiave Wi-Fi (documentato nel `batocera.conf` di Batocera, mai provato su un dispositivo).
- [ ] **Avviare l'immagine su un RG SP reale** (microSD con Etcher): è l'unica prova che parte e che SHARE si espande.
- [ ] Misurare i tempi di Docker su macOS con un'immagine da 9 GB.
- [ ] Provare la finestra "Scegli i giochi" su un item reale con più di 1.000 file (oltre il limite mostrato) e con giochi multi-disco veri.
- [ ] **Committare e pushare** il lavoro dei punti 1 e 2 e del login: ultimo commit pubblicato `c407b64`, il resto è solo in locale.

## Limiti noti
- File di dimensione ignota contano 0 byte nel calcolo dello spazio (il log lo segnala).
- Per i sistemi con `extract: true` la dimensione dello zip sottostima lo spazio reale dopo la scompattazione.
- L'adattamento al disco "salta e prova il successivo": favorisce i giochi piccoli.
- La finestra dei giochi mostra al massimo 1.000 risultati per volta.
- Il raggruppamento multi-disco riconosce solo `(Disc N)`, `(CD N)`, `(Track N)` nel nome.
