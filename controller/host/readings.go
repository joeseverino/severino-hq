package host

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
)

// Reported is every unit of the shipped set systemd can be asked about by
// name: each shipped unit that is not a template, and each instance of a
// shipped template that a shipped file starts (a timer's or path's Unit=, a
// unit's OnFailure=). A template has no state of its own; its instances do.
func Reported(dir string, shipped []string) ([]string, error) {
	seen := map[string]bool{}
	for _, name := range shipped {
		if !strings.Contains(name, "/") && !strings.Contains(name, "@.") {
			seen[name] = true
		}
	}
	for _, name := range shipped {
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return nil, err
		}
		for line := range strings.Lines(string(data)) {
			value, ok := strings.CutPrefix(strings.TrimSpace(line), "Unit=")
			if !ok {
				value, ok = strings.CutPrefix(strings.TrimSpace(line), "OnFailure=")
			}
			if !ok {
				continue
			}
			for _, started := range strings.Fields(value) {
				if instance(started) && slices.Contains(shipped, template(started)) {
					seen[started] = true
				}
			}
		}
	}
	reported := make([]string, 0, len(seen))
	for name := range seen {
		reported = append(reported, name)
	}
	slices.Sort(reported)
	return reported, nil
}

var unitName = regexp.MustCompile(`^[A-Za-z0-9:_.@-]+$`)

// instance is a unit name made of unit-name characters that starts an
// instance of a template: a@b.service, never a@.service or @b.service.
func instance(name string) bool {
	return unitName.MatchString(name) && strings.Contains(name, "@") &&
		!strings.Contains(name, "@.") && !strings.HasPrefix(name, "@")
}

func template(instance string) string {
	return instance[:strings.Index(instance, "@")] + "@" + instance[strings.LastIndex(instance, "."):]
}

// UnitState asks systemd for UnitProperties of each unit, in UTC with
// instants in seconds, and returns its answer.
func (m *Machine) UnitState(ctx context.Context, units []string) ([]byte, error) {
	if len(units) == 0 {
		return nil, fmt.Errorf("no units to ask about")
	}
	args := append([]string{"show", "--timestamp=unix", "--property=" + strings.Join(UnitProperties, ","), "--"}, units...)
	command := m.Command(ctx, "systemctl", args...)
	command.Env = append(command.Env, "TZ=UTC")
	return command.Output()
}

// Tailnet asks the tailscale daemon this machine is a peer of for one local
// API path. The socket is read and write, so the controller is handed what
// it answered and never the socket.
func Tailnet(ctx context.Context, socket, path string) ([]byte, error) {
	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		}},
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://local-tailscaled.sock"+path, nil)
	if err != nil {
		return nil, err
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("tailscaled answered %d", response.StatusCode)
	}
	return io.ReadAll(io.LimitReader(response.Body, 16<<20))
}

// Firewall is whether the firewall requires HQ's port to arrive on the
// tailnet interface, distilled from the input chain: one boolean and the
// rule behind it, where the ruleset is a map of every way into the machine.
type Firewall struct {
	Record                  string `json:"record"`
	Interface               string `json:"interface"`
	AcceptRequiresInterface bool   `json:"accept_requires_interface"`
	ForeignInterfaceDropped bool   `json:"foreign_interface_dropped"`
	ReadAt                  string `json:"read_at"`
}

var (
	acceptsOnTailnet = regexp.MustCompile(`(?s)iifname "tailscale0".*dport.*accept`)
	dropsElsewhere   = regexp.MustCompile(`(?s)iifname != "tailscale0".*drop`)
)

// ReadFirewall distils the `nft list chain inet host_filter input` output.
func ReadFirewall(chain []byte, now time.Time) ([]byte, error) {
	return json.Marshal(Firewall{
		Record:                  "interface-binding",
		Interface:               "tailscale0",
		AcceptRequiresInterface: acceptsOnTailnet.Match(chain),
		ForeignInterfaceDropped: dropsElsewhere.Match(chain),
		ReadAt:                  now.UTC().Format(time.RFC3339),
	})
}
