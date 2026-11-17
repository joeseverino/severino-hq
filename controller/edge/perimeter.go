package edge

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/netip"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/api"
)

// perimeterReading is what this machine relies on to stay shut, as it is now.
// Each part says whether it could be read: a control this cannot see is
// unknown, which differs from one it has seen to be off. The half that needs
// the container chain's rules is answered from outside instead, by whether
// the public addresses answer.
type perimeterReading struct {
	Record          string `json:"record"`
	FirewallUnit    string `json:"firewall_unit"`
	PublicAddresses string `json:"public_addresses"`
	ReadAt          string `json:"read_at"`
}

func (r Receiver) perimeter(ctx context.Context, stdout io.Writer) error {
	var state bytes.Buffer
	command := r.process(ctx, r.Systemctl, "is-active", "nftables")
	command.Stdout = &state
	// An inactive unit exits non-zero and still says so.
	_ = command.Run()
	unit := strings.TrimSpace(state.String())
	if unit == "" {
		unit = "unknown"
	}
	addresses, err := r.Addresses()
	if err != nil {
		addresses = nil
	}
	return json.NewEncoder(stdout).Encode(perimeterReading{
		Record:          "perimeter",
		FirewallUnit:    unit,
		PublicAddresses: strings.Join(publicAddresses(addresses), ","),
		ReadAt:          r.Now().UTC().Format("2006-01-02T15:04:05Z"),
	})
}

// tailnetRanges are what a tailnet numbers its machines from.
var tailnetRanges = api.MustPrefixes("TailnetRange", "enum")

// publicAddresses is every IPv4 address on an interface that is neither
// private, loopback, link-local nor tailnet: derived, so a machine renumbered
// or given a second address is still described correctly.
func publicAddresses(addresses []net.Addr) []string {
	public := []string{}
	for _, address := range addresses {
		network, ok := address.(*net.IPNet)
		if !ok {
			continue
		}
		ip, ok := netip.AddrFromSlice(network.IP)
		if !ok || !ip.Unmap().Is4() {
			continue
		}
		ip = ip.Unmap()
		if ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || slices.ContainsFunc(tailnetRanges, func(p netip.Prefix) bool { return p.Contains(ip) }) {
			continue
		}
		public = append(public, ip.String())
	}
	return public
}
