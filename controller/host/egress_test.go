package host

import (
	"context"
	"net/netip"
	"os"
	"slices"
	"strings"
	"testing"
)

func runsContainer(call []string) bool {
	return len(call) > 1 && call[0] == "docker" && call[1] == "run"
}

// The table hq-host loads, exactly: a change to what the container may reach
// is a change to this text.
func TestTheEgressTableIsTheRenderedOne(t *testing.T) {
	want := `table inet severino_hq_renovate {
	chain forward {
		type filter hook forward priority filter - 10; policy accept;
		iifname "hq-renovate0" meta nfproto ipv6 drop
		iifname "hq-renovate0" ip saddr != 10.231.0.0/24 drop
		ip saddr 10.231.0.0/24 ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/3 } drop
	}

	chain input {
		type filter hook input priority filter - 10; policy accept;
		iifname "hq-renovate0" drop
		ip saddr 10.231.0.0/24 drop
	}
}
`
	if got := EgressRules(); got != want {
		t.Errorf("rendered\n%s\nwant\n%s", got, want)
	}
	if load := egressLoad(); load != "table inet severino_hq_renovate\ndelete table inet severino_hq_renovate\n"+want {
		t.Errorf("loads\n%s", load)
	}
}

// The resolvers the container is given are public, so the table lets its
// lookups through, and every pool Docker gives a network from by default is
// dropped, so no other container is reachable.
func TestTheResolversAreReachableAndOtherContainersAreNot(t *testing.T) {
	var dropped []netip.Prefix
	for _, cidr := range Unroutable {
		dropped = append(dropped, netip.MustParsePrefix(cidr))
	}
	for _, resolver := range RenovateResolvers {
		address := netip.MustParseAddr(resolver)
		if !address.Is4() || !address.IsGlobalUnicast() || address.IsPrivate() || slices.ContainsFunc(dropped, func(p netip.Prefix) bool { return p.Contains(address) }) {
			t.Errorf("the resolver %s is not a public IPv4 address the table lets through", resolver)
		}
	}
	for _, pool := range []string{"172.17.0.0/16", "172.31.0.0/16", "192.168.0.0/16"} {
		pool := netip.MustParsePrefix(pool)
		if !slices.ContainsFunc(dropped, func(p netip.Prefix) bool { return p.Bits() <= pool.Bits() && p.Contains(pool.Addr()) }) {
			t.Errorf("Docker's default pool %s is a destination the table lets through", pool)
		}
	}
}

func TestAMissingNetworkIsMadeAndTheTableLoadedBeforeTheContainerRuns(t *testing.T) {
	v := newRenovation(t)
	v.network = ""
	v.spool(t, exampleRun("example"))

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	create := []string{"docker", "network", "create", "--driver", "bridge", "--ipv6=false",
		"--subnet", "10.231.0.0/24", "--opt", "com.docker.network.bridge.name=hq-renovate0", "severino-hq-renovate"}
	inspect := []string{"docker", "network", "inspect", "--format", "{{json .}}", "severino-hq-renovate"}
	if len(v.calls) < 5 || !slices.Equal(v.calls[0], inspect) || !slices.Equal(v.calls[1], create) || !slices.Equal(v.calls[2], inspect) ||
		!slices.Equal(v.calls[3], []string{"nft", "-f", "-"}) || !runsContainer(v.calls[4]) {
		t.Fatalf("ran %q", v.calls)
	}
	if loaded, _ := os.ReadFile(v.loaded); string(loaded) != egressLoad() {
		t.Errorf("nft was given %q", loaded)
	}
	run := v.calls[4]
	if network := run[slices.Index(run, "--network")+1]; network != RenovateNetwork {
		t.Errorf("the container runs on %s", network)
	}
}

func TestANetworkThatIsNotTheConfinedOneIsNeverRunOn(t *testing.T) {
	for name, wrong := range map[string][2]string{
		"another subnet":       {`"Subnet":"10.231.0.0/24"`, `"Subnet":"192.0.2.0/24"`},
		"a second subnet":      {`{"Subnet":"10.231.0.0/24","Gateway":"10.231.0.1"}`, `{"Subnet":"10.231.0.0/24"},{"Subnet":"198.51.100.0/24"}`},
		"IPv6 on":              {`"EnableIPv6":false`, `"EnableIPv6":true`},
		"another driver":       {`"Driver":"bridge"`, `"Driver":"macvlan"`},
		"another interface":    {`"hq-renovate0"`, `"br-0123abcd"`},
		"an unreadable answer": {`{"Name"`, `["Name"`},
	} {
		t.Run(name, func(t *testing.T) {
			v := newRenovation(t)
			v.network = strings.Replace(renovateNetworkJSON, wrong[0], wrong[1], 1)
			v.spool(t, exampleRun("example"))

			if err := v.r.Run(context.Background()); err == nil {
				t.Fatal("ran on a network the table does not confine")
			}
			if slices.ContainsFunc(v.calls, func(call []string) bool {
				return runsContainer(call) || call[0] == "nft" || slices.Contains(call, "create")
			}) {
				t.Errorf("ran %q", v.calls)
			}
			v.nothingLeft(t)
		})
	}
}

func TestARunWhoseTableCannotBeLoadedNeverStarts(t *testing.T) {
	v := newRenovation(t)
	v.nftFails = true
	v.spool(t, exampleRun("example"))

	if err := v.r.Run(context.Background()); err == nil || !strings.Contains(err.Error(), EgressTable) {
		t.Fatalf("Run returned %v", err)
	}
	if slices.ContainsFunc(v.calls, runsContainer) {
		t.Errorf("a container ran without its egress table: %q", v.calls)
	}
	v.nothingLeft(t)
}

func TestListingsDifferingOnlyInSpacingAreTheSameRules(t *testing.T) {
	listed := strings.ReplaceAll(EgressRules(), "\t", "    ")
	listed = strings.Replace(listed, "100.64.0.0/10, ", "100.64.0.0/10,\n\t\t\t     ", 1)
	if !sameRules(listed, EgressRules()) {
		t.Error("a rewrapped listing differs")
	}
	if sameRules(strings.Replace(EgressRules(), "10.0.0.0/8, ", "", 1), EgressRules()) {
		t.Error("a listing missing a dropped range is the same")
	}
}
