#!/bin/bash
# Estrae da un'immagine Batocera i file di configurazione predefiniti (batocera.conf e
# es_settings.cfg) dall'immagine di sistema (squashfs) nella partizione di boot.
# Uso: extract_defaults.sh <immagine.img> <cartella_uscita>
# Sola lettura sull'immagine. Va eseguito come root su Linux (in Docker: --privileged).
set -euo pipefail

IMG=$1
OUT=$2
BOOT=/mnt/boot
LOOP=""
cleanup() {
    set +e
    mountpoint -q "$BOOT" && umount "$BOOT"
    [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

[ -e /dev/loop-control ] || mknod -m 0660 /dev/loop-control c 10 237
for i in $(seq 0 15); do [ -e /dev/loop$i ] || mknod -m 0660 /dev/loop$i b 7 $i; done

read -r START SIZE < <(parted -m -s "$IMG" unit B print | awk -F: '$1=="1"{gsub("B","",$2);gsub("B","",$4);print $2, $4}')
[ -n "${START:-}" ] || { echo "partizione di boot non trovata" >&2; exit 1; }
LOOP=$(losetup -f --show -r -o "$START" --sizelimit "$SIZE" "$IMG")
mkdir -p "$BOOT"
mount -t vfat -o ro "$LOOP" "$BOOT"

SQ="$BOOT/boot/batocera"
[ -f "$SQ" ] || { echo "immagine di sistema non trovata: boot/batocera" >&2; exit 1; }

TMP=$(mktemp -d)
D=usr/share/batocera/datainit/system
# batocera.conf e' obbligatorio. es_settings.cfg predefinito puo' non esistere (EmulationStation
# lo crea da solo e vi scrive solo i valori non predefiniti): in quel caso si salta.
unsquashfs -n -d "$TMP/x" "$SQ" "$D/batocera.conf" >/dev/null
[ -f "$TMP/x/$D/batocera.conf" ] || { echo "batocera.conf predefinito non trovato nell'immagine" >&2; exit 1; }
mkdir -p "$OUT"
cp "$TMP/x/$D/batocera.conf" "$OUT/batocera.conf"
rm -rf "$TMP/x"
if unsquashfs -n -d "$TMP/x" "$SQ" "$D/configs/emulationstation/es_settings.cfg" >/dev/null 2>&1 \
   && [ -f "$TMP/x/$D/configs/emulationstation/es_settings.cfg" ]; then
    cp "$TMP/x/$D/configs/emulationstation/es_settings.cfg" "$OUT/es_settings.cfg"
    echo "estratti: batocera.conf es_settings.cfg"
else
    echo "estratto: batocera.conf (nessun es_settings.cfg predefinito)"
fi
rm -rf "$TMP"
