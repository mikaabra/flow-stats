#!/usr/bin/env python3
"""flow-logger.py -- Log conntrack DESTROY events to SQLite.

Reads conntrack -E -e DESTROY -o timestamp, parses each flow to extract
timestamp, proto, remote_ip, port, bytes_up, bytes_down, and inserts into
a SQLite database. Dynamically detects WAN IP and IPv6 PD prefixes.

Aggregates raw flows into flow_agg every 5 minutes with 5-minute buckets
for long-term storage. Retains 24 hours of aggregated data.
"""
import os, signal, sqlite3, subprocess, json, time, ipaddress, select, sys, fcntl

DB_PATH = "/mnt/data/flowstats.db"
PID_PATH = "/tmp/flow-logger.pid"
CONNTRACK_CMD = ["conntrack", "-E", "-e", "DESTROY", "-o", "timestamp",
                 "--buffer-size", "8388608"]
REDETECT_INTERVAL = 300
AGG_INTERVAL = 300      # 5 minutes — how often aggregation runs
BUCKET_INTERVAL = 300   # 5 minutes — bucket size for aggregated data
AGG_LAG = 60            # only aggregate flows > 60 seconds old
CLEANUP_INTERVAL = 3600
MAX_AGE = 24 * 3600
COMMIT_BATCH = 50
MAX_RAW_ROWS = 5000
MAX_AGG_ROWS = 100000
SKIP_PORTS = {'53', '123', '853'}

running = True
local_nets = set()


def log(msg):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"{ts} {msg}\n")


def handle_signal(signum, frame):
    global running
    log(f"Received signal {signum}, shutting down")
    running = False


def group_prefix(ip_str):
    addr = ipaddress.ip_address(ip_str)
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip_str}/{bits}", strict=False))


def check_disk_space():
    """Returns free MB on the DB filesystem, or None on error."""
    try:
        st = os.statvfs(os.path.dirname(DB_PATH) or ".")
        return (st.f_bavail * st.f_frsize) / (1024 * 1024)
    except OSError:
        return None


def detect_local_nets():
    """Detect local networks from multiple sources, tracked independently.
    Returns (static_nets, wan_nets, wan6_nets, ip_nets, wan_ok, wan6_ok, ip_ok)
    where each *_ok is True if that source succeeded. Caller preserves
    previous values for any source that failed."""
    static_nets = set()
    for s in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
              '::1/128', 'fe80::/10', 'fc00::/7']:
        static_nets.add(ipaddress.ip_network(s))
    wan_nets = set()
    wan6_nets = set()
    ip_nets = set()
    wan_ok = wan6_ok = ip_ok = False
    # WAN IPv4 from ubus
    try:
        r = subprocess.run(['ubus', 'call', 'network.interface.wan', 'status'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for a in data.get('ipv4-address', []):
                try:
                    wan_nets.add(ipaddress.ip_network(f"{a['address']}/32"))
                except (ValueError, KeyError):
                    pass
            wan_ok = True
    except Exception as e:
        log(f"WAN ubus detect failed: {e}")
    # IPv6 PD from ubus wan6 — authoritative source for delegated prefixes
    try:
        r = subprocess.run(['ubus', 'call', 'network.interface.wan6', 'status'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for pfx in data.get('ipv6-prefix', []):
                try:
                    pfx_addr = pfx['address']
                    pfx_len = pfx.get('mask', 64)
                    wan6_nets.add(ipaddress.ip_network(
                        f"{pfx_addr}/{pfx_len}", strict=False))
                except (ValueError, KeyError):
                    pass
            wan6_ok = True
    except Exception as e:
        log(f"wan6 ubus detect failed: {e}")
    # Fallback: global IPs from ip addr show (catches WAN IP, interface prefixes)
    try:
        r = subprocess.run(['ip', '-j', 'addr', 'show'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for iface in data:
                for a in iface.get('addr_info', []):
                    fam = a.get('family')
                    ip = a.get('local')
                    if not ip:
                        continue
                    try:
                        if fam == 'inet':
                            addr = ipaddress.ip_address(ip)
                            if not addr.is_private:
                                ip_nets.add(ipaddress.ip_network(f"{ip}/32"))
                        elif fam == 'inet6':
                            addr = ipaddress.ip_address(ip)
                            if addr.is_global:
                                ip_nets.add(ipaddress.ip_network(
                                    f"{ip}/{a['prefixlen']}", strict=False))
                    except (ValueError, KeyError):
                        pass
            ip_ok = True
    except Exception as e:
        log(f"ip addr detect failed: {e}")
    return static_nets, wan_nets, wan6_nets, ip_nets, wan_ok, wan6_ok, ip_ok


def is_local(ip_str, nets):
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def parse_line(line, nets):
    """Parse a conntrack DESTROY line. Returns tuple or None."""
    parts = line.split()
    if len(parts) < 5:
        return None
    try:
        ts = float(parts[0].strip('[]'))
    except ValueError:
        return None
    proto = parts[2]
    group = 0
    src = [None, None]
    dst = [None, None]
    b = [0, 0]
    dp = [None, None]
    for p in parts[3:]:
        if p.startswith('src='):
            group += 1
            if group <= 2:
                src[group - 1] = p[4:]
        elif p.startswith('dst='):
            if 1 <= group <= 2:
                dst[group - 1] = p[4:]
        elif p.startswith('bytes='):
            if 1 <= group <= 2:
                try:
                    b[group - 1] = int(p[6:])
                except ValueError:
                    pass
        elif p.startswith('dport='):
            if 1 <= group <= 2:
                dp[group - 1] = p[6:]
    if group < 2 or not src[0] or not dst[0]:
        return None
    s1, d1 = src[0], dst[0]
    # Skip link-local, multicast, broadcast
    for ip in (s1, d1):
        try:
            a = ipaddress.ip_address(ip)
            if a.is_link_local or a.is_multicast:
                return None
        except ValueError:
            return None
    if s1 == '255.255.255.255' or d1 == '255.255.255.255':
        return None
    port = dp[0] or '-'
    if port in SKIP_PORTS:
        return None
    l1, l2 = is_local(s1, nets), is_local(d1, nets)
    if l1 and not l2:
        return (ts, proto, d1, port, b[0], b[1])
    if not l1 and l2:
        return (ts, proto, s1, port, b[1], b[0])
    return None


def main():
    global running, local_nets
    os.nice(19)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Singleton via flock on PID file — kernel owns the lock semantics
    pid_fd = os.open(PID_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(pid_fd)
        log("Another instance is running, exiting")
        sys.exit(0)
    os.ftruncate(pid_fd, 0)
    os.write(pid_fd, str(os.getpid()).encode())

    db = None
    proc = None
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            av = db.execute("PRAGMA auto_vacuum").fetchone()[0]
            if av != 2:
                log(f"Warning: auto_vacuum is {av}, expected 2 (INCREMENTAL). DB may need recreation.")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA journal_size_limit=10485760")
            db.execute("PRAGMA wal_autocheckpoint=1000")
            db.execute("""CREATE TABLE IF NOT EXISTS flows(
                timestamp REAL, proto TEXT, remote_ip TEXT, prefix TEXT, port TEXT,
                bytes_up INTEGER, bytes_down INTEGER)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_time ON flows(timestamp)")
            db.execute("""CREATE TABLE IF NOT EXISTS flow_agg(
                bucket REAL, prefix TEXT, proto TEXT, sample_ip TEXT,
                bytes_up INTEGER DEFAULT 0, bytes_down INTEGER DEFAULT 0,
                flows INTEGER DEFAULT 0,
                PRIMARY KEY (bucket, prefix, proto))""")
            db.commit()
        except sqlite3.OperationalError as e:
            log(f"SQLite error during setup: {e}")
            db.close()
            sys.exit(1)

        static_nets, wan_nets, wan6_nets, ip_nets, wan_ok, wan6_ok, ip_ok = detect_local_nets()
        if not (wan_ok or wan6_ok or ip_ok):
            log("Warning: all dynamic network detection failed at startup")
        prev_wan = wan_nets
        prev_wan6 = wan6_nets
        prev_ip = ip_nets
        local_nets = static_nets | wan_nets | wan6_nets | ip_nets
        log(f"Startup complete, detected {len(local_nets)} local networks")
        last_detect = last_cleanup = last_agg = time.monotonic()
        insert_count = 0
        disk_full = False

        for attempt in range(10):
            if not running:
                log("Shutdown during conntrack retry, exiting")
                sys.exit(0)
            try:
                proc = subprocess.Popen(CONNTRACK_CMD, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
                break
            except FileNotFoundError:
                if attempt == 0:
                    log("conntrack not found, retrying...")
                time.sleep(5)
        if proc is None:
            log("Error: conntrack not available after 10 retries, exiting")
            sys.exit(1)

        try:
            # Set large pipe buffer and nonblocking reads
            try:
                fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, 1048576)
            except OSError:
                pass
            fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)

            buf = b''
            while running:
                ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                if ready:
                    try:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                    except BlockingIOError:
                        chunk = None
                    if chunk is None:
                        pass  # EAGAIN, fall through to timers
                    elif not chunk:
                        log("conntrack process exited")
                        break
                    else:
                        buf += chunk
                        # Process all complete lines from this chunk
                        while b'\n' in buf:
                            line_bytes, buf = buf.split(b'\n', 1)
                            if disk_full:
                                continue
                            line = line_bytes.decode('utf-8', errors='replace').strip()
                            if not line:
                                continue
                            r = parse_line(line, local_nets)
                            if r:
                                ts, proto, remote_ip, port, up, down = r
                                prefix = group_prefix(remote_ip)
                                try:
                                    db.execute("INSERT INTO flows VALUES (?,?,?,?,?,?,?)",
                                               (ts, proto, remote_ip, prefix, port, up, down))
                                    insert_count += 1
                                    if insert_count >= COMMIT_BATCH:
                                        db.commit()
                                        insert_count = 0
                                except sqlite3.OperationalError as e:
                                    log(f"SQLite error on insert: {e}")
                                    insert_count = 0
                                    try:
                                        db.rollback()
                                    except sqlite3.OperationalError:
                                        pass
                # Timer section — ALWAYS runs, regardless of whether data was received
                now = time.monotonic()
                if now - last_detect > REDETECT_INTERVAL:
                    s, wan_nets, wan6_nets, ip_nets, wan_ok, wan6_ok, ip_ok = detect_local_nets()
                    if wan_ok:
                        prev_wan = wan_nets
                    if wan6_ok:
                        prev_wan6 = wan6_nets
                    if ip_ok:
                        prev_ip = ip_nets
                    local_nets = s | prev_wan | prev_wan6 | prev_ip
                    if wan_ok or wan6_ok or ip_ok:
                        log(f"WAN redetect: {len(local_nets)} local networks"
                            f" (wan={'ok' if wan_ok else 'fail'},"
                            f" wan6={'ok' if wan6_ok else 'fail'},"
                            f" ip={'ok' if ip_ok else 'fail'})")
                    else:
                        log("WAN redetect: all sources failed, keeping previous networks")
                    last_detect = now
                if now - last_agg > AGG_INTERVAL:
                    free_mb = check_disk_space()
                    if free_mb is None:
                        log("Warning: cannot stat DB filesystem, treating as disk full")
                        disk_full = True
                        last_agg = now
                    elif free_mb < 25:
                        log(f"Warning: only {free_mb:.1f} MB free on DB filesystem, pausing inserts and skipping aggregation")
                        disk_full = True
                        last_agg = now
                    else:
                        disk_full = False
                        cutoff = time.time() - AGG_LAG
                        try:
                            # Aggregate eligible flows first (preserves all old data)
                            db.execute("""
                                INSERT INTO flow_agg (bucket, prefix, proto, sample_ip, bytes_up, bytes_down, flows)
                                SELECT
                                    CAST(timestamp / ? AS INTEGER) * ? AS bucket,
                                    prefix, proto, MIN(remote_ip),
                                    COALESCE(SUM(bytes_up), 0), COALESCE(SUM(bytes_down), 0), COUNT(*)
                                FROM flows
                                WHERE timestamp < ?
                                GROUP BY bucket, prefix, proto
                                ON CONFLICT(bucket, prefix, proto) DO UPDATE SET
                                    bytes_up = flow_agg.bytes_up + excluded.bytes_up,
                                    bytes_down = flow_agg.bytes_down + excluded.bytes_down,
                                    flows = flow_agg.flows + excluded.flows
                            """, (BUCKET_INTERVAL, BUCKET_INTERVAL, cutoff))
                            db.execute("DELETE FROM flows WHERE timestamp < ?", (cutoff,))
                            # Emergency cap: only cap what's left after aggregation
                            raw_count = db.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
                            if raw_count > MAX_RAW_ROWS:
                                db.execute("DELETE FROM flows WHERE rowid IN (SELECT rowid FROM flows ORDER BY timestamp ASC LIMIT ?)",
                                           (raw_count - MAX_RAW_ROWS,))
                                log(f"Emergency cap: deleted {raw_count - MAX_RAW_ROWS} excess raw flows")
                            db.commit()
                            log(f"Aggregation: aggregated and deleted raw flows older than {cutoff}")
                            # Cap flow_agg rows to prevent unbounded growth between hourly cleanups
                            agg_count = db.execute("SELECT COUNT(*) FROM flow_agg").fetchone()[0]
                            if agg_count > MAX_AGG_ROWS:
                                excess = agg_count - MAX_AGG_ROWS
                                db.execute("""DELETE FROM flow_agg WHERE rowid IN (
                                    SELECT rowid FROM flow_agg ORDER BY bucket ASC LIMIT ?
                                )""", (excess,))
                                db.commit()
                                log(f"Agg cap: deleted {excess} excess agg rows (had {agg_count}, cap {MAX_AGG_ROWS})")
                        except sqlite3.OperationalError as e:
                            log(f"SQLite error during aggregation: {e}")
                            try:
                                db.rollback()
                            except sqlite3.OperationalError:
                                pass
                        last_agg = now
                if now - last_cleanup > CLEANUP_INTERVAL:
                    try:
                        cutoff_bucket = (int(time.time() - MAX_AGE) // BUCKET_INTERVAL) * BUCKET_INTERVAL
                        c = db.execute("DELETE FROM flow_agg WHERE bucket < ?",
                                   (cutoff_bucket,))
                        deleted = c.rowcount

                        # Cap flow_agg rows to prevent unbounded growth
                        agg_count = db.execute("SELECT COUNT(*) FROM flow_agg").fetchone()[0]
                        if agg_count > MAX_AGG_ROWS:
                            excess = agg_count - MAX_AGG_ROWS
                            db.execute("""DELETE FROM flow_agg WHERE rowid IN (
                                SELECT rowid FROM flow_agg ORDER BY bucket ASC LIMIT ?
                            )""", (excess,))
                            log(f"Agg cap: deleted {excess} excess agg rows (had {agg_count}, cap {MAX_AGG_ROWS})")

                        db.commit()
                        db.execute("PRAGMA incremental_vacuum")
                        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        db.commit()
                        db_size = os.path.getsize(DB_PATH) / (1024*1024)
                        wal_size = os.path.getsize(DB_PATH + "-wal") / (1024*1024) if os.path.exists(DB_PATH + "-wal") else 0
                        free_mb = check_disk_space()
                        free_str = f"{free_mb:.1f}MB" if free_mb is not None else "unknown"
                        log(f"Cleanup: deleted {deleted} agg rows, DB={db_size:.1f}MB, WAL={wal_size:.1f}MB, free={free_str}")
                    except sqlite3.OperationalError as e:
                        log(f"SQLite error on cleanup: {e}")
                    last_cleanup = now
        finally:
            if proc:
                proc.terminate()
                proc.wait()
            if db:
                try:
                    db.commit()
                except sqlite3.OperationalError as e:
                    log(f"SQLite error on final commit: {e}")
                db.close()
            log("Shutdown complete")
    finally:
        try:
            os.close(pid_fd)
        except OSError:
            pass


if __name__ == "__main__":
    main()
