#!/bin/sh
set -eu

image=/mnt/f/RetroTV-Recovery/RetroTV-D-volume-2026-08-26.img
mountpoint=/mnt/retrotv-recovery-image
mkdir -p "$mountpoint"
cleanup() {
    umount "$mountpoint" 2>/dev/null || true
    rmdir "$mountpoint" 2>/dev/null || true
}
trap cleanup EXIT

mount -t exfat -o loop,ro "$image" "$mountpoint"
find "$mountpoint/Videos/media" -type f -printf '%P\t%s\n' \
    | LC_ALL=C sort > /tmp/retrotv-image-files.tsv
find /mnt/d/Videos/media -type f -printf '%P\t%s\n' \
    | LC_ALL=C sort > /tmp/retrotv-repaired-files.tsv
comm -23 /tmp/retrotv-image-files.tsv /tmp/retrotv-repaired-files.tsv \
    > /tmp/retrotv-missing.tsv
comm -13 /tmp/retrotv-image-files.tsv /tmp/retrotv-repaired-files.tsv \
    > /tmp/retrotv-new.tsv

printf 'IMAGE_COUNT=%s\n' "$(wc -l < /tmp/retrotv-image-files.tsv)"
printf 'REPAIRED_COUNT=%s\n' "$(wc -l < /tmp/retrotv-repaired-files.tsv)"
printf 'MISSING_OR_CHANGED=%s\n' "$(wc -l < /tmp/retrotv-missing.tsv)"
printf 'NEW_OR_CHANGED=%s\n' "$(wc -l < /tmp/retrotv-new.tsv)"
printf 'MISSING_BYTES=%s\n' "$(awk -F '\t' '{sum += $2} END {printf "%.0f", sum}' /tmp/retrotv-missing.tsv)"
printf '%s\n' '--- MISSING SAMPLE ---'
head -100 /tmp/retrotv-missing.tsv
printf '%s\n' '--- NEW SAMPLE ---'
head -30 /tmp/retrotv-new.tsv
