#!/bin/bash
# Allarga la partizione SHARE (ext4, ultima dell'immagine) e ci copia le ROM.
# Uso: build_image.sh <immagine.img> <cartella_stage> <dimensione_share_in_byte>
# Va eseguito come root su Linux (in Docker: --privileged).
set -euo pipefail

IMG=$1
STAGE=$2
SHARE_BYTES=$3
MNT=/mnt/share
LOOP=""

cleanup() {
    set +e
    mountpoint -q "$MNT" && umount "$MNT"
    [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

# In un container i nodi /dev/loopN non compaiono da soli.
[ -e /dev/loop-control ] || mknod -m 0660 /dev/loop-control c 10 237
for i in $(seq 0 15); do [ -e /dev/loop$i ] || mknod -m 0660 /dev/loop$i b 7 $i; done

# Offset/dimensione (in byte) della partizione 2.
part2() {
    parted -m -s "$IMG" unit B print | awk -F: '$1=="2"{gsub("B","",$2);gsub("B","",$4);print $2, $4}'
}

read -r START _ < <(part2)
[ -n "${START:-}" ] || { echo "partizione 2 non trovata in $IMG" >&2; exit 1; }

NEW_SIZE=$((START + SHARE_BYTES))
CUR_SIZE=$(stat -c %s "$IMG")
if (( NEW_SIZE > CUR_SIZE )); then
    echo "==> allargo l'immagine a $NEW_SIZE byte"
    truncate -s "$NEW_SIZE" "$IMG"
fi

echo "==> allargo la partizione SHARE"
echo ',+' | sfdisk --quiet --no-reread --no-tell-kernel -N 2 "$IMG"

read -r START SIZE < <(part2)
LOOP=$(losetup -f --show -o "$START" --sizelimit "$SIZE" "$IMG")

echo "==> controllo e ridimensiono il filesystem"
e2fsck -fy "$LOOP" || [ $? -le 1 ]
resize2fs "$LOOP"

mkdir -p "$MNT"
mount "$LOOP" "$MNT"

echo "==> copio le ROM"
rsync -rlt --chmod=D755,F644 "$STAGE"/ "$MNT"/
sync
umount "$MNT"

echo "==> verifica finale"
e2fsck -fn "$LOOP"
echo "==> fatto"
