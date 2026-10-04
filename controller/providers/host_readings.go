package providers

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Readings of the host the controller runs on: its firewall and its perimeter.

// PublishedContainer is a container the sweep found and the ports it publishes.
// Host is the environment's connection ref, HostAddress its address.
type PublishedContainer struct {
	Host        string
	HostAddress string
	Ports       []int
}

// HostPerimeterRecord is what one edge relies on to stay shut, and whether it is.
type HostPerimeterRecord struct {
	Record           string   `json:"record"`
	ConnectionRef    string   `json:"connection_ref"`
	FirewallUnit     string   `json:"firewall_unit"`
	PublicAddresses  []string `json:"public_addresses"`
	PortsChecked     []int    `json:"ports_checked"`
	AnsweredPublicly []int    `json:"answered_publicly"`
	ReadAt           string   `json:"read_at"`
}

// perimeterReading is what the perimeter operation prints on the edge.
type perimeterReading struct {
	PublicAddresses string `json:"public_addresses"`
	FirewallUnit    string `json:"firewall_unit"`
	ReadAt          string `json:"read_at"`
}

func (r *Registry) admitHostReadings() {
	r.reader(runtime.ResourceKindHostFirewall, r.hostFirewall)
	r.reader(runtime.ResourceKindHostPerimeter, r.hostPerimeter)
}

// HostFirewallRecord is the distilled firewall answer run-controller.sh mounts.
type HostFirewallRecord struct {
	Record                  string `json:"record"`
	Interface               string `json:"interface"`
	AcceptRequiresInterface bool   `json:"accept_requires_interface"`
	ForeignInterfaceDropped bool   `json:"foreign_interface_dropped"`
	ReadAt                  string `json:"read_at"`
}

// hostFirewall is whether HQ's port must arrive on the tailnet interface. Root
// reads the ruleset and mounts one distilled answer; this never reads the rules.
func (r *Registry) hostFirewall(context.Context) ([]any, error) {
	path := r.Env["SEVERINO_HOST_FIREWALL"]
	if path == "" {
		return nil, &ProviderError{Message: "no host firewall reading was mounted"}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, &ProviderError{Message: "read host firewall reading", Err: err}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var record HostFirewallRecord
	if err := decoder.Decode(&record); err != nil {
		return nil, &ProviderError{Message: "decode host firewall reading", Err: err}
	}
	if record.Record == "" {
		return nil, &ProviderError{Message: "host firewall reading names no record"}
	}
	return []any{record}, nil
}

// hostPerimeter asks each Caddy host for its firewall unit and public addresses,
// then connects to those addresses from here on the ports its containers
// publish and on SSH, which must answer only on the tailnet.
func (r *Registry) hostPerimeter(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.Env.RoleRefs("caddy") {
		output, err := r.commands().SSH(ctx, ref, "perimeter", nil)
		if err != nil {
			return nil, err
		}
		var reading perimeterReading
		if len(output) > 0 {
			if err := json.Unmarshal(output, &reading); err != nil {
				return nil, &ProviderError{Message: "SSH perimeter for " + ref + ": decode reading", Err: err}
			}
		}
		addresses := []string{}
		for _, address := range strings.Split(reading.PublicAddresses, ",") {
			if address = strings.TrimSpace(address); address != "" {
				addresses = append(addresses, address)
			}
		}
		target, err := r.Env.SSH(ref)
		if err != nil {
			return nil, err
		}
		at := map[string]bool{target.Host: true}
		for _, address := range addresses {
			at[address] = true
		}
		ports := r.publishedPortsAt(ctx, ref, at)
		ports[standardSSHPort], ports[target.Port] = true, true
		checked := sortedPorts(ports)
		found = append(found, HostPerimeterRecord{
			Record:           "perimeter",
			ConnectionRef:    ref,
			FirewallUnit:     orDefault(reading.FirewallUnit, "unknown"),
			PublicAddresses:  addresses,
			PortsChecked:     checked,
			AnsweredPublicly: r.answered(ctx, addresses, checked),
			ReadAt:           reading.ReadAt,
		})
	}
	return found, nil
}

// publishedPortsAt is the ports containers on one machine publish. A container
// is on it when its environment carries the connection's name or answers at
// one of the machine's addresses. Nothing to check where nothing describes it.
func (r *Registry) publishedPortsAt(ctx context.Context, ref string, addresses map[string]bool) map[int]bool {
	ports := map[int]bool{}
	if r.PublishedContainers == nil {
		return ports
	}
	containers, err := r.PublishedContainers(ctx)
	if err != nil {
		return ports
	}
	at := map[string]bool{ref: true}
	for address := range addresses {
		at[address] = true
	}
	delete(at, "")
	for _, container := range containers {
		if !at[container.Host] && !at[container.HostAddress] {
			continue
		}
		for _, port := range container.Ports {
			if port > 0 && port < 65536 {
				ports[port] = true
			}
		}
	}
	return ports
}

func sortedPorts(ports map[int]bool) []int {
	out := make([]int, 0, len(ports))
	for port := range ports {
		out = append(out, port)
	}
	sort.Ints(out)
	return out
}

// answered is which ports accepted a TCP connection on any of the addresses.
func (r *Registry) answered(ctx context.Context, addresses []string, ports []int) []int {
	dial := r.Dial
	if dial == nil {
		dial = dialFromHere
	}
	var mu sync.Mutex
	open := map[int]bool{}
	var wg sync.WaitGroup
	for _, address := range addresses {
		for _, port := range ports {
			wg.Add(1)
			go func(address string, port int) {
				defer wg.Done()
				if dial(ctx, address, port) {
					mu.Lock()
					open[port] = true
					mu.Unlock()
				}
			}(address, port)
		}
	}
	wg.Wait()
	return sortedPorts(open)
}

// dialFromHere is whether a TCP connection is accepted, asked from this machine,
// which reaches a public address the way anybody else would.
func dialFromHere(ctx context.Context, address string, port int) bool {
	dialer := net.Dialer{Timeout: perimeterDialTimeout}
	conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(address, strconv.Itoa(port)))
	if err != nil {
		return false
	}
	conn.Close()
	return true
}
