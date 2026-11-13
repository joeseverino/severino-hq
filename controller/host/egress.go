package host

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"strings"
)

// A confined container's network is a bridge of its own, so its egress table
// can name the container's traffic by interface and by source. Each subnet
// lies outside every pool Docker allocates from by default (172.17.0.0/16 to
// 172.31.0.0/16, and 192.168.0.0/16) and outside the LAN and the tailnet, so
// no other network on the host is given it; a create that overlaps one fails.
const (
	RenovateNetwork = "severino-hq-renovate"
	// EgressTable is the Renovate run's nftables table, which hq-host owns:
	// replaced whole before each run, never edited in place.
	EgressTable = "severino_hq_renovate"
	// AutomationNetwork is the automation worker's network, and
	// AutomationSubnet its one declaration of the addresses its containers
	// take. No machine the worker logs in to sees them: a connection leaving
	// the host is masqueraded, and one to the host itself is given the
	// host's own address (confinement.rules).
	AutomationNetwork = "severino-hq-automation"
	AutomationSubnet  = "10.231.1.0/24"
	bridgeName        = "com.docker.network.bridge.name"
)

// confinement is a container network and the nftables table that holds what
// the network's containers may reach: public IPv4 addresses, and the private
// destinations in reach alone, each one TCP address and port.
type confinement struct {
	network, bridge, subnet, table string
	reach                          []netip.AddrPort
}

var renovateEgress = confinement{network: RenovateNetwork, bridge: "hq-renovate0", subnet: "10.231.0.0/24", table: EgressTable}

// automationEgress is the automation worker's network, before the runner
// connections it reaches are added.
var automationEgress = confinement{network: AutomationNetwork, bridge: "hq-automation0", subnet: AutomationSubnet, table: "severino_hq_automation"}

// PublicResolvers are what a confined container resolves names with, public
// so its lookups take the only path it is allowed: Docker's embedded resolver
// dials an upstream that is not on the host's loopback from inside the
// container's namespace, where a LAN or tailnet resolver is dropped.
var PublicResolvers = []string{"1.1.1.1", "8.8.8.8"}

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

// EgressRules is the Renovate run's table as `nft list table` prints it,
// which a preflight compares with the one loaded.
func EgressRules() string { return renovateEgress.rules() }

// rules is the table. Forward drops what the container sends anywhere but a
// public IPv4 address or a destination in reach, IPv6 and a forged source
// included; input drops everything it sends to the host itself but a
// destination in reach, which is one of the host's own addresses. Both run
// before Docker's filter (priority filter): a drop in any base chain is
// final, so nothing Docker accepts later lets the packet through.
//
// A destination in reach that is the host itself is then given the host's
// own address as its source, by the nat chain on the input hook: the gates
// of a login there (sshd's from= and AllowUsers, a PAM check of the source's
// tailnet node) know the host by its address, never by the bridge's subnet,
// and the address grants nothing a process on the host lacks. Only a packet
// delivered locally traverses the input hook, so the map, which gives each
// destination in reach itself as the source, rewrites no other; one the host
// forwards leaves through Docker's masquerade instead. The chain runs at
// srcnat, after every filter has seen the bridge's address. The kernel
// admits snat on the input hook of an inet nat chain (net/netfilter/nft_nat.c
// nft_nat_validate, nft_chain_nat.c nft_chain_nat_inet), nftables accepts
// srcnat there (src/rule.c std_prio_family_hook_compat), and nft(8)'s "NAT
// STATEMENTS" gives the map form.
func (c confinement) rules() string {
	reach, source := "", ""
	if len(c.reach) > 0 {
		elements := make([]string, len(c.reach))
		sources := make([]string, len(c.reach))
		for i, destination := range c.reach {
			elements[i] = fmt.Sprintf("%s . %d", destination.Addr(), destination.Port())
			sources[i] = fmt.Sprintf("%s : %s", elements[i], destination.Addr())
		}
		reach = fmt.Sprintf("\n\t\tip saddr %s ip daddr . tcp dport { %s } accept", c.subnet, strings.Join(elements, ", "))
		source = fmt.Sprintf("\n\n\tchain source {\n\t\ttype nat hook input priority srcnat; policy accept;\n\t\tiifname %q ip saddr %s snat ip to ip daddr . tcp dport map { %s }\n\t}",
			c.bridge, c.subnet, strings.Join(sources, ", "))
	}
	return fmt.Sprintf(`table inet %[1]s {
	chain forward {
		type filter hook forward priority filter - 10; policy accept;
		iifname "%[2]s" meta nfproto ipv6 drop
		iifname "%[2]s" ip saddr != %[3]s drop%[5]s
		ip saddr %[3]s ip daddr { %[4]s } drop
	}

	chain input {
		type filter hook input priority filter - 10; policy accept;%[5]s
		iifname "%[2]s" drop
		ip saddr %[3]s drop
	}%[6]s
}
`, c.table, c.bridge, c.subnet, strings.Join(Unroutable, ", "), reach, source)
}

// load is what `nft -f -` is given: the table declared, so the delete cannot
// fail on a host without it, deleted, and declared afresh, in one
// transaction that applies whole or not at all.
func (c confinement) load() string {
	return "table inet " + c.table + "\ndelete table inet " + c.table + "\n" + c.rules()
}

// sameRules compares two listings of a table, ignoring how nft spaces and
// wraps them.
func sameRules(listed, rendered string) bool {
	return strings.Join(strings.Fields(listed), " ") == strings.Join(strings.Fields(rendered), " ")
}

// confine readies the only network a run may use: the network made if absent
// and refused unless it is exactly the one the table names, then the table
// loaded. A run never starts without both.
func (c confinement) confine(ctx context.Context, m *Machine) error {
	found, err := c.inspectNetwork(ctx, m)
	if !found {
		create := []string{"network", "create", "--driver", "bridge", "--ipv6=false",
			"--subnet", c.subnet, "--opt", bridgeName + "=" + c.bridge, c.network}
		if err := m.run(ctx, "docker", create...); err != nil {
			return err
		}
		_, err = c.inspectNetwork(ctx, m)
	}
	if err != nil {
		return err
	}
	load := m.Command(ctx, "nft", "-f", "-")
	load.Stdin = strings.NewReader(c.load())
	if output, err := load.CombinedOutput(); err != nil {
		return fmt.Errorf("the egress table %s could not be loaded: %w: %s", c.table, err, output)
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
func (c confinement) inspectNetwork(ctx context.Context, m *Machine) (bool, error) {
	out, err := m.Command(ctx, "docker", "network", "inspect", "--format", "{{json .}}", c.network).Output()
	if err != nil {
		return false, fmt.Errorf("the network %s cannot be inspected: %w", c.network, err)
	}
	var network dockerNetwork
	if err := json.Unmarshal(out, &network); err != nil {
		return true, fmt.Errorf("the network %s: %w", c.network, err)
	}
	subnets := make([]string, 0, len(network.IPAM.Config))
	for _, config := range network.IPAM.Config {
		subnets = append(subnets, config.Subnet)
	}
	if network.Driver != "bridge" || network.EnableIPv6 || len(subnets) != 1 || subnets[0] != c.subnet || network.Options[bridgeName] != c.bridge {
		return true, fmt.Errorf("the network %s is driver %q, IPv6 %t, subnets %v, interface %q; a run needs bridge, no IPv6, %s and %s: remove it and the next run makes it",
			c.network, network.Driver, network.EnableIPv6, subnets, network.Options[bridgeName], c.subnet, c.bridge)
	}
	return true, nil
}
