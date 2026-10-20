# Site-specific files (author's setup — not needed by most users)

These files exist for quirks of the author's deployment: OpenWrt as a
Proxmox x86 VM behind a Bahnhof (SE) fiber hand-off via a dumb media
converter, plus WAN-side nftables byte counters. They are not part of
flow-stats proper and are unlikely to apply to your network.

## ISP NDP workaround (Bahnhof VLAN 4004)

Bahnhof delivers ALL NDP traffic (NS/NA) tagged with VLAN 4004 with
PCP 7; everything else — RAs, DHCPv6, data — arrives untagged. Without
help the kernel drops the tagged frames and IPv6 neighbor resolution
never completes in either direction. Two runtime objects fix it:

| File | Deploys to | Purpose |
|------|-----------|---------|
| `99-wan-neighbor` | `/etc/hotplug.d/iface/99-wan-neighbor` | on wan6 ifup: create `eth1.4004` (kernel can then accept tagged NDP) and pin a static PERMANENT neighbor for the ISP gateway on the untagged path |
| `20-eth1-4004-ndp.nft` | `/etc/nftables.d/20-eth1-4004-ndp.nft` | allow NDP packet types on `eth1.4004`, which belongs to no firewall zone |

Notes from the 2026-10-20 incident (IPv6 dead after a sysupgrade):

- A sysupgrade wipes custom `/etc/hotplug.d` files (and possibly
  `/etc/nftables.d`) unless keep-listed in `/etc/sysupgrade.conf`.
  Symptom of a lost workaround: `ip -6 neigh` shows only incomplete
  entries, lan clients keep GUAs but nothing routes, and flow-stats
  fills with zero-byte v6 rows.
- The ISP gateway link-local is NOT MAC-derived. Never derive its MAC
  via EUI-64 (`5a:d0:...` is wrong); capture it on the wire with
  `tcpdump -i eth1 'vlan 4004'` (`58:d0:61:11:d2:f7` here).
- The ISP spams an unsolicited NA roughly every second — the data path
  only needs the static neighbor; unanswered v6 NS probes from the
  kernel on `eth1.4004` are harmless.

## WAN protocol counters

| File | Deploys to | Purpose |
|------|-----------|---------|
| `30-wan-protocol-counters.nft` | `/etc/nftables.d/30-wan-protocol-counters.nft` | nftables byte counters by protocol on the WAN interface — independent cross-check for the conntrack-based flow stats |
