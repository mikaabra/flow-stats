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
  inserts into a SQLite database (`/mnt/data/flowstats.db`) with WAL
  mode. Dynamically detects WAN IP and IPv6 PD prefixes every 5
  minutes. The `flows` table is a short-lived buffer only: every 5
  minutes its rows older than 60 s are folded into long-term `flow_agg`
  5-minute buckets and deleted. `flow_agg` is what the report reads and
  is retained 24 hours (hourly cleanup).

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
| `wan-stats.sh` | `/usr/local/bin/wan-stats.sh` | WAN IPv4 vs IPv6 traffic summary (forwarded) |

Files in [`site-specific/`](site-specific/) (author's ISP/VM setup —
VLAN 4004 NDP workaround, WAN counters) are excluded from this table
intentionally; most setups won't need them.

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
- MaxMind GeoLite2-ASN MMDB at `/mnt/data/GeoLite2-ASN.mmdb`
  (system works without it -- reports "unknown" for ASN)
- Kernel conntrack accounting enabled (`nf_conntrack_acct=1`)
- **Software flow offloading must stay DISABLED**
  (`firewall.@defaults[0].flow_offloading='0'`). Offloaded connections
  do not emit conntrack DESTROY events until teardown or an external
  conntrack flush — bulk flows (video streaming, large downloads)
  silently vanish from the stats, sometimes for days. Verified
  empirically on OpenWrt 25.12: with offloading enabled, a 50 MB
  download produced no DESTROY after many minutes; flushed conntrack
  showed the same flow with full byte counters. On x86 the CPU cost of
  offload-free forwarding is negligible.


## MMDB Setup

The GeoLite2-ASN database requires a free MaxMind license key and your
numeric Account ID:

1. Sign up at https://www.maxmind.com/en/geolite2/signup
2. Save your key: `echo 'YOUR_KEY' > /etc/maxmind-license.key && chmod 600 /etc/maxmind-license.key`
3. Save your Account ID: `echo YOUR_ACCOUNT_ID > /etc/maxmind-account.id && chmod 600 /etc/maxmind-account.id`
4. Run: `update-mmdb.sh`

Downloads use MaxMind's current download API
(`https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download`,
HTTP Basic auth `account_id:license_key`). The endpoint 302-redirects
to a Cloudflare R2 presigned URL which must be fetched WITHOUT the
Authorization header — Python's urllib forwards auth headers across
redirects (curl -L strips them) and R2 rejects such requests, so the
script blocks the redirect and fetches the presigned URL credential-free.

**Key format caveat**: MaxMind license keys may use a grouped format
with internal spaces. Preserve them exactly as pasted — only trim
whitespace at the line ends. Whole-string whitespace removal (e.g.
`tr -d '[:space:]'`) mangles the key and every download fails with 401
Unauthorized.

Without the MMDB file, the system works but reports "unknown" for all
ASN lookups.

## Persistence

Database and MMDB paths are hardcoded to `/mnt/data`, so stats live on
a dedicated data disk and survive reboots. None of that is automatic —
wire it up or lose it:

1. **Mount the data disk at boot**: `apk add block-mount`, then create
   an `/etc/config/fstab` mount entry (target `/mnt/data`). UCI config
   survives sysupgrade; a hand-run `mount` does not.
2. **Start the logger at boot**: `/etc/init.d/flow-logger enable`
3. **Keep custom files across sysupgrade**: add the scripts,
   `/etc/init.d/flow-logger`, and `/mnt/data` to
   `/etc/sysupgrade.conf`. Sysupgrade (ext4) restores only keep-listed
   files — everything else on the rootfs is replaced.

A hand-mounted disk, a hand-started service, or a mountpoint directory
outside the keep list all vanish silently on the next reboot or
sysupgrade — check mounts, `ps`, and rc.d symlinks after any reflash.

---

## Keywords

netflow, ipfix, flow monitoring, traffic analysis, traffic accounting,
bandwidth monitoring, asn, autonomous system number, bgp, ip-to-asn,
conntrack, nf_conntrack, netfilter, openwrt, router traffic, who is using my bandwidth,
per-asn traffic, network visibility, flow export, geoip, maxmind, geolite2,
ipv6, prefix delegation, traffic by asn, internet traffic breakdown
