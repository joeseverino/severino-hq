package host

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A host with tailscaled enabled has a tailnet whether or not the daemon's
// socket is there at this moment: HQ is given the runtime directory either
// way, so a daemon away for a restart makes a call wait rather than run
// unplaced. A host without the unit has no tailnet.
func TestAHostWithTailscaledEnabledAlwaysGivesHQTheTailnet(t *testing.T) {
	root := t.TempDir()
	unit := filepath.Join(root, "multi-user.target.wants", "tailscaled.service")
	run := filepath.Join(root, "tailscale")
	runner := &WebRunner{TailscaledUnit: unit, TailscaleRun: run}

	if runner.tailnet() {
		t.Error("a host without tailscaled enabled has a tailnet")
	}
	if err := os.MkdirAll(filepath.Dir(unit), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("/usr/lib/systemd/system/tailscaled.service", unit); err != nil {
		t.Fatal(err)
	}
	if !runner.tailnet() {
		t.Error("a host with tailscaled enabled, its socket away, has no tailnet")
	}
	args := strings.Join(Web{Image: "ghcr.io/example/hq@" + digest, TailscaleRun: run}.Args(), " ")
	if !strings.Contains(args, "--env SEVERINO_TAILSCALED_SOCKET="+TailscaledSocket) {
		t.Errorf("HQ is not told where the socket is: %s", args)
	}
}
