#!/bin/sh
# Show IPv4 vs IPv6 traffic on WAN (eth1, forwarded only)
fmt() {
    b=$1
    if [ "$b" -gt 1073741824 ]; then
        awk "BEGIN {printf \"%.2f GB\", $b/1073741824}"
    elif [ "$b" -gt 1048576 ]; then
        awk "BEGIN {printf \"%.2f MB\", $b/1048576}"
    elif [ "$b" -gt 1024 ]; then
        awk "BEGIN {printf \"%.2f KB\", $b/1024}"
    else
        echo "$b B"
    fi
}

pct() {
    if [ "$2" -gt 0 ]; then
        awk "BEGIN {printf \"%5.1f\", 100*$1/$2}"
    else
        printf "%5.1f" 0
    fi
}

# Parse nft counters
v4rx_p=0; v4rx_b=0; v4tx_p=0; v4tx_b=0
v6rx_p=0; v6rx_b=0; v6tx_p=0; v6tx_b=0

parse_counter() {
    name=$1
    nft list counter inet fw4 $name 2>/dev/null | grep "counter" | head -1
}

line=$(nft list counter inet fw4 ipv4_wan_rx 2>/dev/null)
v4rx_p=$(echo "$line" | grep -o "packets [0-9]*" | grep -o "[0-9]*")
v4rx_b=$(echo "$line" | grep -o "bytes [0-9]*" | grep -o "[0-9]*")

line=$(nft list counter inet fw4 ipv4_wan_tx 2>/dev/null)
v4tx_p=$(echo "$line" | grep -o "packets [0-9]*" | grep -o "[0-9]*")
v4tx_b=$(echo "$line" | grep -o "bytes [0-9]*" | grep -o "[0-9]*")

line=$(nft list counter inet fw4 ipv6_wan_rx 2>/dev/null)
v6rx_p=$(echo "$line" | grep -o "packets [0-9]*" | grep -o "[0-9]*")
v6rx_b=$(echo "$line" | grep -o "bytes [0-9]*" | grep -o "[0-9]*")

line=$(nft list counter inet fw4 ipv6_wan_tx 2>/dev/null)
v6tx_p=$(echo "$line" | grep -o "packets [0-9]*" | grep -o "[0-9]*")
v6tx_b=$(echo "$line" | grep -o "bytes [0-9]*" | grep -o "[0-9]*")

total_rx=$((v4rx_b + v6rx_b))
total_tx=$((v4tx_b + v6tx_b))
total_v4=$((v4rx_b + v4tx_b))
total_v6=$((v6rx_b + v6tx_b))
total=$((total_rx + total_tx))

echo "WAN Traffic Statistics (eth1, forwarded only)"
echo "=============================================="
echo
echo "           IPv4              IPv6              Total"
echo
printf "  RX  %8s (%s%%)  %8s (%s%%)  %8s\n" "$(fmt $v4rx_b)" "$(pct $v4rx_b $total_rx)" "$(fmt $v6rx_b)" "$(pct $v6rx_b $total_rx)" "$(fmt $total_rx)"
printf "  TX  %8s (%s%%)  %8s (%s%%)  %8s\n" "$(fmt $v4tx_b)" "$(pct $v4tx_b $total_tx)" "$(fmt $v6tx_b)" "$(pct $v6tx_b $total_tx)" "$(fmt $total_tx)"
echo
printf "  Total:  IPv4 %s (%s%%)  IPv6 %s (%s%%)  %s\n" "$(fmt $total_v4)" "$(pct $total_v4 $total)" "$(fmt $total_v6)" "$(pct $total_v6 $total)" "$(fmt $total)"
echo
echo "  Packets: IPv4 RX/TX $v4rx_p/$v4tx_p  IPv6 RX/TX $v6rx_p/$v6tx_p"
