#!/usr/bin/env python3
"""flow-report.py -- Traffic report by prefix and ASN.

Usage: flow-report.py [hours] [-4|-6] [-s total|down|up|flows] [--prefix|--asn]

Queries SQLite for flows in the last N hours, groups by /24 (IPv4) or
/48 (IPv6), looks up ASN via MaxMind GeoLite2-ASN MMDB, and prints a
sorted table with summary.  By default, rows are consolidated by ASN.
Use --prefix to show individual /24 or /48 prefixes.
"""
import sys, os, time, sqlite3, ipaddress

DB_PATH = "/mnt/data/flowstats.db"
MMDB_PATH = "/mnt/data/GeoLite2-ASN.mmdb"
SEP = "=" * 67
DASH = "-" * 107


def hr(b):
    if b >= 1073741824:
        return f"{b / 1073741824:.2f} GB"
    if b >= 1048576:
        return f"{b / 1048576:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{b} B"


def parse_args():
    hours, affilter, sortkey, asn_mode = 24, "", "total", True
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--help", "-h"):
            print(__doc__ or "Usage: flow-report.py [hours] [-4|-6] [-s total|down|up|flows] [--prefix|--asn]")
            sys.exit(0)
        elif a == "-4":
            affilter = "4"
        elif a == "-6":
            affilter = "6"
        elif a == "-s" and i + 1 < len(args):
            i += 1
            sortkey = args[i]
            if sortkey not in ("total", "down", "up", "flows"):
                print(f"Invalid sort key: {sortkey}", file=sys.stderr)
                sys.exit(2)
        elif a == "--prefix":
            asn_mode = False
        elif a == "--asn":
            asn_mode = True
        else:
            try:
                hours = int(a)
            except ValueError:
                pass
        i += 1
    return hours, affilter, sortkey, asn_mode


def group_prefix(ip_str):
    addr = ipaddress.ip_address(ip_str)
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip_str}/{bits}", strict=False))


def open_mmdb():
    try:
        import maxminddb
        return maxminddb.open_database(MMDB_PATH)
    except Exception:
        return None


def lookup_asn(reader, ip_str):
    if reader is None:
        return "unknown", "unknown"
    try:
        r = reader.get(ip_str)
        if r:
            asn = r.get('autonomous_system_number')
            if asn is None:
                asn = 'unknown'
            name = r.get('autonomous_system_organization')
            if name is None:
                name = 'unknown'
            return asn, name
    except Exception:
        pass
    return "unknown", "unknown"


def main():
    hours, affilter, sortkey, asn_mode = parse_args()
    now = time.time()
    cutoff = (int(now - hours * 3600) // 300) * 300

    if not os.path.exists(DB_PATH):
        print(f"No flows in the last {hours} hours")
        return

    db = None
    reader = None
    groups = {}
    total_bytes = total_flows = 0
    ipv4_bytes = ipv6_bytes = 0
    proto_bytes = {}

    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        reader = open_mmdb()
        try:
            # Check actual data range
            actual_cutoff_row = db.execute("SELECT MIN(bucket) FROM flow_agg WHERE bucket >= ?", (cutoff,)).fetchone()
            actual_cutoff = actual_cutoff_row[0] if actual_cutoff_row[0] is not None else cutoff
            if actual_cutoff > cutoff:
                ps = time.strftime("%Y-%m-%d %H:%M", time.localtime(actual_cutoff))
            else:
                ps = time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff))

            cursor = db.execute("""
                SELECT prefix, proto, MIN(sample_ip) AS sample_ip,
                       SUM(bytes_up) as up, SUM(bytes_down) as down, SUM(flows) as flows
                FROM flow_agg
                WHERE bucket >= ?
                GROUP BY prefix, proto
            """, (cutoff,))
        except sqlite3.OperationalError:
            print(f"No flows in the last {hours} hours")
            return

        # Phase 1: aggregate by prefix (merge protocols)
        for prefix, proto, sample_ip, up, down, flows in cursor:
            if affilter == "4" and ":" in prefix:
                continue
            if affilter == "6" and ":" not in prefix:
                continue
            up = int(up or 0)
            down = int(down or 0)
            total = up + down
            if prefix not in groups:
                groups[prefix] = {"total": 0, "down": 0, "up": 0, "flows": 0,
                                "sample": sample_ip}
            g = groups[prefix]
            g["total"] += total
            g["down"] += down
            g["up"] += up
            g["flows"] += int(flows or 0)
            proto_bytes[proto] = proto_bytes.get(proto, 0) + total
            total_bytes += total
            total_flows += int(flows or 0)
            if ":" in prefix:
                ipv6_bytes += total
            else:
                ipv4_bytes += total

        if not groups:
            print(f"No flows in the last {hours} hours")
            return

        # Phase 2: ASN lookup and collapse by ASN (or show per-prefix)
        if asn_mode:
            asn_groups = {}
            for prefix, d in groups.items():
                asn, name = lookup_asn(reader, d["sample"])
                key = f"AS{asn}" if asn != "unknown" else "unknown"
                if key not in asn_groups:
                    asn_groups[key] = {"total": 0, "down": 0, "up": 0, "flows": 0,
                                       "asn": f"AS{asn}" if asn != "unknown" else "unknown",
                                       "name": name or "", "prefix": prefix, "best": 0,
                                       "prefixes": set()}
                g = asn_groups[key]
                g["total"] += d["total"]
                g["down"] += d["down"]
                g["up"] += d["up"]
                g["flows"] += d["flows"]
                g["prefixes"].add(prefix)
                if d["total"] > g["best"]:
                    g["best"] = d["total"]
                    g["prefix"] = prefix
        else:
            # --prefix mode: one row per prefix, with ASN as metadata
            asn_groups = {}
            for prefix, d in groups.items():
                asn, name = lookup_asn(reader, d["sample"])
                asn_groups[prefix] = {"total": d["total"], "down": d["down"],
                                      "up": d["up"], "flows": d["flows"],
                                      "asn": f"AS{asn}" if asn != "unknown" else "unknown",
                                      "name": name or "", "prefix": prefix,
                                      "prefixes": set([prefix])}

        # Phase 3: sort + top 25
        sk = {"total": "total", "down": "down", "up": "up",
              "flows": "flows"}.get(sortkey, "total")
        sorted_all = sorted(asn_groups.items(), key=lambda x: x[1][sk],
                              reverse=True)
        sorted_rows = sorted_all[:25]
        max_bytes = max(r[1]["total"] for r in sorted_rows) if sorted_rows else 0

        # Calculate "Other" row from remaining entries
        other_rows = sorted_all[25:]
        other = None
        if other_rows:
            other = {"total": 0, "down": 0, "up": 0, "flows": 0,
                     "asn": "Other", "name": "", "prefixes": set(),
                     "best_prefix": "", "best_total": 0}
            for key, d in other_rows:
                other["total"] += d["total"]
                other["down"] += d["down"]
                other["up"] += d["up"]
                other["flows"] += d["flows"]
                other["prefixes"].update(d["prefixes"])
                if d["total"] > other["best_total"]:
                    other["best_total"] = d["total"]
                    other["best_prefix"] = d["prefix"]
            label = 'ASN' if asn_mode else 'prefix'
            count = len(other_rows)
            if count == 1:
                other["name"] = f"({count} {label})"
            elif asn_mode:
                other["name"] = f"({count} ASNs)"
            else:
                other["name"] = f"({count} prefixes)"

        # Print report
        title = f"Traffic Report -- Last {hours}h"
        if affilter:
            title += f" (IPv{affilter} only)"
        if sortkey != "total":
            title += f" -- sorted by {sortkey}"
        if not asn_mode:
            title += " -- prefix view"
        pe = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        print(SEP)
        print(f"  {title}")
        print(f"  Period: {ps} -- {pe}")
        print(f"  Total Flows: {total_flows} | Total Traffic: {hr(total_bytes)}")
        print(SEP)
        print()
        prefix_hdr = "Prefixes" if asn_mode else "Prefix"
        print(f"{'Rank':<4}  {'ASN':<8} {'AS Name':<22} {prefix_hdr:<26} "
              f"{'Flows':>6}  {'Download':>10}  {'Upload':>10}  {'Total':>10}  Bar")
        print(DASH)

        for i, (key, d) in enumerate(sorted_rows, 1):
            bar_len = (d["total"] * 30 // max_bytes) if max_bytes > 0 else 0
            bar = "\u2588" * bar_len
            name = (d["name"] or "")[:22]
            if asn_mode:
                pc = len(d["prefixes"])
                prefix_str = f"{pc} prefix{'es' if pc != 1 else ''}"
            else:
                prefix_str = d["prefix"]
            print(f"{i:<4}  {d['asn']:<8} {name:<22} {prefix_str:<26} "
                  f"{d['flows']:>6}  {hr(d['down']):>10}  {hr(d['up']):>10}  "
                  f"{hr(d['total']):>10}  {bar}")

        if other:
            name = other["name"][:22]
            if asn_mode:
                pc = len(other["prefixes"])
                prefix_str = f"{pc} prefix{'es' if pc != 1 else ''}"
            else:
                prefix_str = other["best_prefix"] or "-"
            print(f"{'...':<4}  {other['asn']:<8} {name:<22} {prefix_str:<26} "
                  f"{other['flows']:>6}  {hr(other['down']):>10}  {hr(other['up']):>10}  "
                  f"{hr(other['total']):>10}  ")

        print()
        print(DASH)
        print("Summary")
        print(f"  Total Traffic: {hr(total_bytes)}")
        print("  By Protocol:")
        for proto in sorted(proto_bytes, key=lambda p: proto_bytes[p], reverse=True):
            pb = proto_bytes[proto]
            pct = pb * 100 // total_bytes if total_bytes > 0 else 0
            print(f"    {proto:<8} {hr(pb)} ({pct}%)")
        if not affilter:
            v4pct = ipv4_bytes * 100 // total_bytes if total_bytes > 0 else 0
            v6pct = ipv6_bytes * 100 // total_bytes if total_bytes > 0 else 0
            print(f"  IPv4: {hr(ipv4_bytes)} ({v4pct}%)")
            print(f"  IPv6: {hr(ipv6_bytes)} ({v6pct}%)")
        elif affilter == "4":
            print(f"  IPv4: {hr(ipv4_bytes)} (100%)")
        else:
            print(f"  IPv6: {hr(ipv6_bytes)} (100%)")
        print(SEP)

    finally:
        if reader:
            reader.close()
        if db:
            db.close()


if __name__ == "__main__":
    main()
