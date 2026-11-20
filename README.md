# console-maker

Crea un'immagine **Batocera** con le tue ROM già dentro, pronta da scrivere con balenaEtcher su microSD/disco.

Dispositivi: Anbernic RG SP, Raspberry Pi 3/4/5, PC x86_64 (altri modelli: una riga in `cm/distro.py`).

## Requisiti
- Python 3.10+
- Docker (su macOS) per modificare l'immagine; su Linux senza Docker basta `sudo`

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp roms.example.yaml roms.yaml     # poi compilalo (vedi sotto)
.venv/bin/python console_maker.py
```

## Uso
Si apre una schermata unica con i menu: dispositivo, dimensione del disco, console da includere (spazio = scegli, `a` = tutte), opzioni. Premi **Avvia**: scarica Batocera e le ROM, crea l'immagine in `out/`. Le scelte vengono ricordate in `config.yaml`.

### roms.yaml
Per ogni console/emulatore indichi i file da scaricare. La chiave è il nome della cartella Batocera:

```yaml
systems:
  nes:
    name: Nintendo NES
    urls:
      - https://archive.org/download/<identificativo>/<file>.zip
  mame:
    name: Arcade (MAME)
    urls: [...]
  atari2600:
    extract: true      # lo zip è un set completo da scompattare
    urls: [...]
```

Invece di elencare ogni file puoi indicare un intero item di archive.org:

```yaml
  psx:
    name: PlayStation
    item: <identificativo>          # archive.org/details/<identificativo>
    include: '\.chd$'               # facoltativi: regex sul nome file
    exclude: '\((Demo|Beta|Proto)'
```

Lo script legge l'elenco dei file da archive.org (con dimensioni). Con **Scegli i giochi** nella schermata apri l'elenco del sistema evidenziato, cerchi per nome e spunti solo quelli che vuoi: i file di uno stesso gioco (cue+bin, dischi multipli, m3u) restano insieme. La scelta va in `selections.yaml` (non tocca `roms.yaml`). Un item senza filtri né scelta scarica tutto, quindi controlla il totale mostrato.

### Download veloci (più connessioni)
archive.org limita la velocità per singola connessione, quindi ogni file viene scaricato **a pezzi in parallelo** (4 connessioni di default; nei miei test su archive.org una sola connessione dava ~0,09 MB/s e quattro ~0,6 MB/s, otto non miglioravano). Prima di partire una richiesta di prova verifica che il server accetti davvero gli intervalli: se no, si scarica come prima in un solo flusso. I file sotto 16 MB si scaricano in un solo flusso. Se l'operazione si interrompe, alla ripresa si rifanno solo i pezzi mancanti (`*.part` e `*.part.state` in `cache/`). Per cambiare il numero di connessioni (1-16, 1 = flusso unico) modifica `connections` in `config.yaml`.

### BIOS
Molti emulatori (PlayStation, Saturn, Neo Geo...) non partono senza BIOS. Nella sezione `bios:` di `roms.yaml` ogni gruppo ha `urls` o `item` come i sistemi, più `dir` (sottocartella di `userdata/bios`, vuota = direttamente in `bios/`) e `for` (i sistemi per cui servono). Un gruppo con `for` entra nell'immagine solo se almeno uno di quei sistemi c'è ancora dopo l'adattamento al disco; senza `for` entra sempre. I BIOS contano nello spazio e non vengono mai sfoltiti. L'opzione "Non includere i file BIOS" li esclude.

### Barre di avanzamento
Durante l'esecuzione la schermata mostra due barre: il **file in corso** (nome, byte, velocità, tempo stimato) e il **totale di ROM e BIOS** (file e GB completati, velocità e tempo stimato complessivi). Il totale si basa sulle dimensioni dichiarate da archive.org: se mancano, la barra conta i file. L'immagine Batocera ha solo la barra del file in corso, perché si scarica prima.

### Item ad accesso riservato (login archive.org)
Alcuni item (collezioni "loggedin") rispondono 401 a chi non è collegato. Il pulsante **Login archive.org...** apre una finestra con email e password: la password va solo a archive.org e **non viene salvata**; si conserva la sessione in `~/.config/console-maker/archive_auth.json` (permessi 600, fuori dalla repo). I cookie di sessione vengono mandati solo ad archive.org e ai suoi server dei dati, in HTTPS, mai a Batocera né ad altri siti. **Disconnetti** cancella il file. In alternativa lo strumento usa le variabili `IA_LOGGED_IN_USER` / `IA_LOGGED_IN_SIG` oppure la configurazione di `ia` se c'è già. Un account non basta sempre: alcuni item si possono solo prendere in prestito, e in quel caso il download dà un 401 con un messaggio dedicato.

### Se non ci stanno
Con l'opzione **"Se non ci stanno, togli i giochi meno importanti"** (attiva di default) lo script legge la dimensione esatta dell'immagine base e, prima di scaricare le ROM, toglie quelle che non entrano nel disco indicato. Decidi cosa si tiene col campo `priority` di ogni sistema in `roms.yaml` (più alto = tenuto per primo; a parità conta l'ordine nel file). Nel log vedi quanti giochi sono stati tolti per sistema. Senza l'opzione, se non ci stanno si ferma con un errore. Limiti: i file di dimensione ignota contano 0 byte, e per i sistemi con `extract: true` la dimensione dello zip sottostima lo spazio reale.

Il file viene validato all'avvio (URL non validi, chiavi sconosciute, duplicati). Rispetta il copyright: scarica solo ciò che hai il diritto di usare.

## Come funziona
1. Cerca l'ultima immagine sul mirror ufficiale (`stable/last`, altrimenti `stable/rc`). Per l'**RG SP** al momento esiste solo la release candidate.
2. Scarica con ripresa e verifica l'md5. Cache in `cache/`.
3. Allarga la partizione SHARE (ext4) dell'immagine quanto serve alle ROM e le copia in `roms/<sistema>/`.
4. Scrive `out/batocera-<dispositivo>-<versione>-<data>-roms.img`.

La dimensione del disco serve solo a verificare che tutto ci stia: al primo avvio Batocera espande SHARE fino a riempire la scheda. L'immagine base pesa circa 8,5 GB (partizione di boot da 8 GB).

## Copertine, immagini e video
Non li scarica lo script: al primo avvio usa lo scraper di Batocera (START → Impostazioni scraper). Serve la rete sul dispositivo; un account ScreenScraper gratuito alza i limiti.

## Test
```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
I test end-to-end usano Docker e un'immagine sintetica; non scaricano nulla.
