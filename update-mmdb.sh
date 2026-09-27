#!/bin/sh
# update-mmdb.sh -- Download/update GeoLite2-ASN MMDB database
#
# Requires a free MaxMind license key. Get one at:
#   https://www.maxmind.com/en/geolite2/signup
# Save your key to /etc/maxmind-license.key

KEYFILE=/etc/maxmind-license.key
MMDB=/mnt/data/GeoLite2-ASN.mmdb
MMDB_LINK=/etc/GeoLite2-ASN.mmdb
TMPDIR=$(mktemp -d /tmp/mmdb-download.XXXXXX) || exit 1

cleanup() {
    rm -rf "$TMPDIR"
    rm -f "$MMDB.tmp" 2>/dev/null
}
trap cleanup EXIT INT TERM

if [ ! -f "$KEYFILE" ]; then
    echo "Error: MaxMind license key not found at $KEYFILE"
    echo ""
    echo "To get a free license key:"
    echo "  1. Sign up at https://www.maxmind.com/en/geolite2/signup"
    echo "  2. Save your key:"
    echo "     echo 'YOUR_KEY' > $KEYFILE"
    echo "     chmod 600 $KEYFILE"
    echo "  3. Run this script again"
    exit 1
fi

KEY=$(cat "$KEYFILE" | tr -d '[:space:]')

cd "$TMPDIR" || exit 1

echo "Downloading GeoLite2-ASN database..."
wget -q -T 30 -O geolite2-asn.tar.gz \
    "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-ASN&license_key=${KEY}&suffix=tar.gz"

if [ $? -ne 0 ]; then
    echo "Error: Download failed"
    rm -f geolite2-asn.tar.gz
    exit 1
fi

if [ ! -s geolite2-asn.tar.gz ]; then
    echo "Error: Download failed (empty file)"
    rm -f geolite2-asn.tar.gz
    exit 1
fi

if ! tar tzf geolite2-asn.tar.gz >/dev/null 2>&1; then
    echo "Error: Downloaded file is not a valid gzip archive"
    rm -f geolite2-asn.tar.gz
    exit 1
fi

echo "Extracting..."
tar xzf geolite2-asn.tar.gz

rm -f geolite2-asn.tar.gz

MMDB_FILE=$(ls -1 */GeoLite2-ASN.mmdb 2>/dev/null | head -1)
if [ -z "$MMDB_FILE" ]; then
    echo "Error: GeoLite2-ASN.mmdb not found in archive"
    rm -f geolite2-asn.tar.gz
    exit 1
fi

case "$MMDB_FILE" in
    *..* ) echo "Error: suspicious path in archive"; exit 1 ;;
esac
# Also check it's a regular file, not a symlink
if [ -L "$MMDB_FILE" ]; then
    echo "Error: extracted file is a symlink, refusing to use"
    exit 1
fi

cp "$MMDB_FILE" "$MMDB.tmp"
if [ $? -ne 0 ]; then
    echo "Error: copy failed"
    exit 1
fi
chmod 644 "$MMDB.tmp"
mv "$MMDB.tmp" "$MMDB"
if [ $? -ne 0 ]; then
    echo "Error: move failed"
    exit 1
fi
ln -sf "$MMDB" "$MMDB_LINK"
rm -rf "$TMPDIR"
echo "Done: $MMDB updated"
