# flow-stats

Lightweight traffic flow monitoring for OpenWrt using conntrack DESTROY events.

Attributes network traffic to remote Autonomous Systems (ASNs) — answers
"where is my bandwidth going?" without a full NetFlow/IPFIX exporter or
flow collector stack.

## What it does

Captures per-flow byte counts from the kernel conntrack table (via
`conntrack -E -e DESTROY -o timestamp`) and stores them in a SQLite
database. A report script aggregates by remote destination prefix
(/24 for IPv4, /48 for IPv6), enriches with ASN information from a
local MaxMind GeoLite2-ASN MMDB database, and collapses by ASN.

## Architecture

- **Logger** (`flow-logger.py`): Python3 daemon that reads conntrack
  DESTROY events directly (no awk, no FIFO), parses each flow, and
  inserts into a SQLite database (`/tmp/flowstats.db`) with WAL mode.
  Dynamically detects WAN IP and IPv6 PD prefixes every 60 seconds.
  Cleans up records older than 48 hours every hour.

- **Report** (`flow-report.py`): Queries SQLite for flows in the last
  N hours, groups by /24 or /48, looks up ASN using the MaxMind MMDB
  library, collapses by ASN, and prints a sorted table with summary.

- **MMDB update** (`update-mmdb.sh`): Downloads/updates the GeoLite2-ASN
  MMDB database using a MaxMind license key.

## Files

| File | Deploys to | Purpose |
|------|-----------|---------|
| `flow-logger.py` | `/usr/local/bin/flow-logger.py` | Daemon: conntrack -> SQLite |
| `flow-report.py` | `/usr/local/bin/flow-report.py` | Report: SQLite -> prefix/ASN aggregation |
| `flow-report.sh` | `/usr/local/bin/flow-report.sh` | Wrapper: exec python3 flow-report.py |
| `flow-logger.init` | `/etc/init.d/flow-logger` | procd init script (respawn, nice 19) |
| `update-mmdb.sh` | `/usr/local/bin/update-mmdb.sh` | MMDB database download/update |

## Usage

```sh
# Start the logger (auto-starts on boot once enabled)
/etc/init.d/flow-logger enable
/etc/init.d/flow-logger start

# Report: last 24 hours, all traffic
flow-report.sh 24

# Report: last 6 hours, IPv6 only
flow-report.sh 6 -6

# Report: last 1 hour, IPv4 only, sorted by flow count
flow-report.sh 1 -4 -s flows

# Update MMDB database
update-mmdb.sh
```

## Requirements

- OpenWrt 25.x with `conntrack` CLI (`apk add conntrack`)
- Python 3 (`apk add python3`)
- `python3-maxminddb` for ASN lookups (`apk add python3-maxminddb`)
- MaxMind GeoLite2-ASN MMDB at `/etc/GeoLite2-ASN.mmdb`
  (system works without it -- reports "unknown" for ASN)
- Kernel conntrack accounting enabled (`nf_conntrack_acct=1`)

## MMDB Setup

The GeoLite2-ASN database requires a free MaxMind license key:

1. Sign up at https://www.maxmind.com/en/geolite2/signup
2. Save your key: `echo 'YOUR_KEY' > /etc/maxmind-license.key && chmod 600 /etc/maxmind-license.key`
3. Run: `update-mmdb.sh`

Without the MMDB file, the system works but reports "unknown" for all
ASN lookups.

---

## Keywords

netflow, ipfix, flow monitoring, traffic analysis, traffic accounting,
bandwidth monitoring, asn, autonomous system number, bgp, ip-to-asn,
conntrack, nf_conntrack, netfilter, openwrt, router traffic, who is using my bandwidth,
per-asn traffic, network visibility, flow export, geoip, maxmind, geolite2,
ipv6, prefix delegation, traffic by asn, internet traffic breakdown
