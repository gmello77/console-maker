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
