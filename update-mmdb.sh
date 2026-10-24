#!/bin/sh
# update-mmdb.sh -- Download/update GeoLite2-ASN MMDB database
#
# Requires a free MaxMind license key. Get one at:
#   https://www.maxmind.com/en/geolite2/signup
# Save your key to /etc/maxmind-license.key

KEYFILE=/etc/maxmind-license.key
ACCOUNTFILE=/etc/maxmind-account.id
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

# Trim whitespace at line ends ONLY. MaxMind license keys use a grouped
# format with internal spaces; tr -d '[:space:]' (or similar whole-string
# whitespace removal) mangles the key and the download fails with 401.
KEY=$(head -n1 "$KEYFILE" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

if [ -n "$MAXMIND_ACCOUNT_ID" ]; then
    ACC="$MAXMIND_ACCOUNT_ID"
elif [ -f "$ACCOUNTFILE" ]; then
    ACC=$(head -n1 "$ACCOUNTFILE" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -z "$ACC" ]; then
        echo "Error: $ACCOUNTFILE exists but is empty"
        exit 1
    fi
else
    echo "Error: MaxMind Account ID not found."
    echo "Save the numeric Account ID (from your MaxMind account portal):"
    echo "  echo YOUR_ACCOUNT_ID > $ACCOUNTFILE"
    echo "  chmod 600 $ACCOUNTFILE"
    echo "or export MAXMIND_ACCOUNT_ID"
    exit 1
fi

cd "$TMPDIR" || exit 1

echo "Downloading GeoLite2-ASN database..."
# New MaxMind download API (migrated 2026-10-24; legacy /app/geoip_download
# endpoint retired from use): https://download.maxmind.com/geoip/databases/
# <Edition>/download?suffix=tar.gz with Basic auth account_id:license_key.
# GOTCHA: the endpoint 302-redirects to an R2 presigned URL which must be
# fetched WITHOUT the Authorization header — Python urllib forwards auth
# headers across redirects (curl -L does not), and R2 rejects any request
# bearing a non-SigV4 Authorization header with "Missing
# x-amz-content-sha256". Hence the manual two-hop dance below: block the
# redirect, capture Location, fetch it credential-free. Error text stays
# key-free. Exit status propagates to the $? check below.
# Account ID: numeric, from the MaxMind account portal (GeoIP.conf); read
# from $MAXMIND_ACCOUNT_ID or $ACCOUNTFILE. The license key lives only in
# $KEYFILE (0600).
MMACC="$ACC" MMKEY="$KEY" python3 -c 'import os, sys, base64
import urllib.request, urllib.error
auth = base64.b64encode((os.environ["MMACC"] + ":" + os.environ["MMKEY"]).encode()).decode()
url = "https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download?suffix=tar.gz"
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
opener = urllib.request.build_opener(NoRedirect)
req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
presigned = None
try:
    resp = opener.open(req, timeout=30)
except urllib.error.HTTPError as e:
    if e.code in (301, 302, 303, 307, 308):
        presigned = e.headers.get("Location")
        e.close()
    else:
        print("download error:", type(e).__name__, e.code, str(e)[:120])
        sys.exit(1)
try:
    src = urllib.request.urlopen(presigned, timeout=120) if presigned else resp
    with open(sys.argv[1], "wb") as f:
        while True:
            chunk = src.read(65536)
            if not chunk: break
            f.write(chunk)
except Exception as e:
    print("download error:", type(e).__name__, str(e)[:120])
    sys.exit(1)' geolite2-asn.tar.gz

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
