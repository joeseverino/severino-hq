package host

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

// The Renovate container's network: a bridge of its own, so the egress table
// can name its traffic by interface and by source. The subnet lies outside
// every pool Docker allocates from by default (172.17.0.0/16 to
// 172.31.0.0/16, and 192.168.0.0/16) and outside the LAN and the tailnet, so
// no other network on the host is given it; a create that overlaps one fails.
const (
	RenovateNetwork = "severino-hq-renovate"
	renovateBridge  = "hq-renovate0"
	renovateSubnet  = "10.231.0.0/24"
	// EgressTable is the nftables table hq-host owns: replaced whole before
	// each run, never edited in place.
	EgressTable = "severino_hq_renovate"
	bridgeName  = "com.docker.network.bridge.name"
)

// RenovateResolvers are public resolvers, so the container's lookups take the
// only path it is allowed: Docker's embedded resolver dials an upstream that
// is not on the host's loopback from inside the container's namespace, where
// a LAN or tailnet resolver is dropped.
var RenovateResolvers = []string{"1.1.1.1", "8.8.8.8"}

// Unroutable is every IPv4 destination the container may not reach through
// the host: this-network, RFC 1918, the tailnet's CGNAT range, loopback,
// link-local, and multicast with the reserved range above it. The host's own
// addresses are the input chain's: a packet bound for one never reaches the
// forward hook.
var Unroutable = []string{
	"0.0.0.0/8",
	"10.0.0.0/8",
	"100.64.0.0/10",
	"127.0.0.0/8",
	"169.254.0.0/16",
	"172.16.0.0/12",
	"192.168.0.0/16",
	"224.0.0.0/3",
}

// EgressRules is the table as `nft list table` prints it. Forward drops what
// the container sends anywhere but a public IPv4 address, IPv6 and a forged
// source included; input drops everything it sends to the host itself. Both
// run before Docker's filter (priority filter): a drop in any base chain is
// final, so nothing Docker accepts later lets the packet through.
func EgressRules() string {
	return fmt.Sprintf(`table inet %[1]s {
	chain forward {
		type filter hook forward priority filter - 10; policy accept;
		iifname "%[2]s" meta nfproto ipv6 drop
		iifname "%[2]s" ip saddr != %[3]s drop
		ip saddr %[3]s ip daddr { %[4]s } drop
	}

	chain input {
		type filter hook input priority filter - 10; policy accept;
		iifname "%[2]s" drop
		ip saddr %[3]s drop
	}
}
`, EgressTable, renovateBridge, renovateSubnet, strings.Join(Unroutable, ", "))
}

// egressLoad is what `nft -f -` is given: the table declared, so the delete
// cannot fail on a host without it, deleted, and declared afresh, in one
// transaction that applies whole or not at all.
func egressLoad() string {
	return "table inet " + EgressTable + "\ndelete table inet " + EgressTable + "\n" + EgressRules()
}

// sameRules compares two listings of a table, ignoring how nft spaces and
// wraps them.
func sameRules(listed, rendered string) bool {
	return strings.Join(strings.Fields(listed), " ") == strings.Join(strings.Fields(rendered), " ")
}

// confine readies the only network a run may use: the network made if absent
// and refused unless it is exactly the one the table names, then the table
// loaded. A run never starts without both.
func (r *Renovator) confine(ctx context.Context) error {
	found, err := r.inspectNetwork(ctx)
	if !found {
		create := []string{"network", "create", "--driver", "bridge", "--ipv6=false",
			"--subnet", renovateSubnet, "--opt", bridgeName + "=" + renovateBridge, RenovateNetwork}
		if err := r.run(ctx, "docker", create...); err != nil {
			return err
		}
		_, err = r.inspectNetwork(ctx)
	}
	if err != nil {
		return err
	}
	load := r.Command(ctx, "nft", "-f", "-")
	load.Stdin = strings.NewReader(egressLoad())
	if output, err := load.CombinedOutput(); err != nil {
		return fmt.Errorf("the egress table %s could not be loaded: %w: %s", EgressTable, err, output)
	}
	return nil
}

// dockerNetwork is what the confinement depends on in `docker network
// inspect`.
type dockerNetwork struct {
	Driver     string
	EnableIPv6 bool
	IPAM       struct{ Config []struct{ Subnet string } }
	Options    map[string]string
}

// inspectNetwork is whether docker answers for the network, and an error
// unless it is the one the egress table expects: a bridge with IPv6 off, one
// subnet, and the interface the table names. A network docker cannot inspect
// is one to create; a create that finds it after all fails.
func (r *Renovator) inspectNetwork(ctx context.Context) (bool, error) {
	out, err := r.Command(ctx, "docker", "network", "inspect", "--format", "{{json .}}", RenovateNetwork).Output()
	if err != nil {
		return false, fmt.Errorf("the network %s cannot be inspected: %w", RenovateNetwork, err)
	}
	var network dockerNetwork
	if err := json.Unmarshal(out, &network); err != nil {
		return true, fmt.Errorf("the network %s: %w", RenovateNetwork, err)
	}
	subnets := make([]string, 0, len(network.IPAM.Config))
	for _, config := range network.IPAM.Config {
		subnets = append(subnets, config.Subnet)
	}
	if network.Driver != "bridge" || network.EnableIPv6 || len(subnets) != 1 || subnets[0] != renovateSubnet || network.Options[bridgeName] != renovateBridge {
		return true, fmt.Errorf("the network %s is driver %q, IPv6 %t, subnets %v, interface %q; a run needs bridge, no IPv6, %s and %s: remove it and the next run makes it",
			RenovateNetwork, network.Driver, network.EnableIPv6, subnets, network.Options[bridgeName], renovateSubnet, renovateBridge)
	}
	return true, nil
}
