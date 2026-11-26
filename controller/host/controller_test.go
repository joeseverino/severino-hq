package host

import (
	"bytes"
	"context"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
	"github.com/joeseverino/severino-hq/controller/secrets/project"
)

// Values that may reach the container only inside the mounted document.
const (
	sentinel  = "sentinel-provider-token-value"
	reference = "sentinel-connection"
)

// launchVariables is every variable the launcher may set: a path, a name, an
// image or a nonce.
var launchVariables = []string{
	"HQ_CONTROLLER_RUNS", "SEVERINO_BRIDGE_SOCKET", "HQ_CONTROLLER_CONNECTIONS",
	"HQ_CONTROLLER_SSH_DIR", "HQ_ACME_DIR", "HQ_SEAL_DIR", "HQ_CONTROLLER_IMAGE", "SEVERINO_HQ_SOURCE_REPOSITORY",
	"HQ_CONTROLLER_CA_FILE", "SEVERINO_TAILNET_STATUS", "SEVERINO_TAILNET_LOCK",
	"SEVERINO_HOST_FIREWALL", "SEVERINO_RENDER_STATUS", "SEVERINO_HOST_UNITS", "SEVERINO_HOST_RESTARTING_UNITS", "SEVERINO_HOST_FILESYSTEMS", "SEVERINO_HOST_REBOOT",
	"HQ_CONTROLLER_OUT", "SEVERINO_DEPENDENCY_RUN", "SEVERINO_UNIT_SOCKET", "HQ_CONTROLLER_GRANTS",
}

type launch struct {
	f          *fakeHost
	controller *Controller
	args       []string
	mounted    map[string][]byte
	// lentFiles is every file in the ssh directory the container is lent.
	lentFiles []string
	locked    bool
	order     []string
	// container is what the pass does, given its out directory on the host.
	container func(out string)
	// loaded is whether an egress table was loaded.
	loaded bool
}

// listenerNetworkJSON is what `docker network inspect` says of the
// listener's network, as Docker prints one it made.
const listenerNetworkJSON = `{"Name":"severino-hq-listener","Id":"89abcdef","Scope":"local","Driver":"bridge","EnableIPv4":true,"EnableIPv6":false,` +
	`"IPAM":{"Driver":"default","Options":null,"Config":[{"Subnet":"10.231.2.0/24","Gateway":"10.231.2.1"}]},"Internal":false,"Attachable":false,` +
	`"Containers":{},"Options":{"com.docker.network.bridge.name":"hq-listener0"},"Labels":{}}`

func newLaunch(t *testing.T) *launch {
	t.Helper()
	f := newFakeHost(t)
	f.running = running
	secrets := filepath.Join(f.root, "secrets")
	if err := os.MkdirAll(filepath.Join(secrets, "ssh"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(secrets, "ssh", reference), []byte("an identity\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	document, err := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{
		{Ref: reference, Provider: "cloudflare_dns", Store: connections.Store{Vault: "Example", Item: "example"},
			APIToken: &connections.APIToken{URL: "https://api.example.com", APIToken: sentinel}},
	}}.Encode()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(secrets, "controller-connections.json"), document, 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(secrets, "web"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(secrets, "web", hostcore.AppEnvName), []byte("DJANGO_SECRET_KEY=sentinel-application-secret\n"), 0o400); err != nil {
		t.Fatal(err)
	}

	l := &launch{f: f, mounted: map[string][]byte{}}
	program := f.machine.Command
	f.machine.Command = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		if name == "docker" && args[0] == "run" {
			l.args = args
			// The run's staging is gone once the launch returns: keep what
			// was mounted while it is still there.
			for i, arg := range args {
				if arg != "--mount" {
					continue
				}
				if source, target, ok := bind(args[i+1]); ok {
					if data, err := os.ReadFile(source); err == nil {
						l.mounted[target] = data
					}
					if entries, err := os.ReadDir(source); err == nil && target == controllerSSH {
						for _, entry := range entries {
							l.lentFiles = append(l.lentFiles, entry.Name())
						}
					}
					if target == controllerOut && l.container != nil {
						l.container(source)
					}
				}
			}
		}
		if name == "docker" && slices.Equal(args[:2], []string{"network", "inspect"}) {
			return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"`, "sh", listenerNetworkJSON)
		}
		if name == "nft" && slices.Equal(args, []string{"-f", "-"}) {
			l.loaded = true
			return exec.CommandContext(ctx, "/bin/sh", "-c", "cat >/dev/null")
		}
		if name == "nft" || name == "systemctl" || name == "busctl" {
			return exec.CommandContext(ctx, "/bin/sh", "-c", "exit 1")
		}
		return program(ctx, name, args...)
	}
	l.controller = &Controller{
		Machine:        f.machine,
		SecretDir:      secrets,
		ACME:           filepath.Join(f.root, "acme"),
		CertStore:      filepath.Join(f.root, "certs"),
		Tailscaled:     filepath.Join(f.root, "absent.sock"),
		UnitSocket:     filepath.Join(f.root, "unit", "unit.sock"),
		Grants:         []Grant{relocated(DependencyUpdates, f.root)},
		DependencyRun:  filepath.Join(f.root, "state", "renovate", "dependency-run.json"),
		MountTable:     filepath.Join(f.root, "absent-mounts"),
		Statfs:         func(string) (free, total uint64, err error) { return 59 << 20, 29 << 30, nil },
		RebootRequired: filepath.Join(f.root, "reboot-required"),
		Born:           func(string) (time.Time, error) { return time.Date(2029, 12, 31, 18, 0, 0, 0, time.UTC), nil },
		Connections:    func() error { return nil },
		Lock: func(context.Context) (func(), error) {
			l.locked = true
			l.order = append(l.order, "lock")
			return func() { l.locked = false; l.order = append(l.order, "unlock") }, nil
		},
		LookPath: func(string) (string, error) { return "", errors.New("absent") },
		Now:      time.Now,
		Stdout:   io.Discard, Stderr: io.Discard,
	}
	return l
}

func bind(mount string) (source, target string, ok bool) {
	found := regexp.MustCompile(`^type=bind,source=([^,]+),target=([^,]+)`).FindStringSubmatch(mount)
	if found == nil {
		return "", "", false
	}
	return found[1], found[2], true
}

func (l *launch) run(t *testing.T, pass Pass) {
	t.Helper()
	if err := l.controller.Run(context.Background(), pass); err != nil {
		t.Fatal(err)
	}
}

func (l *launch) envs() []string {
	var envs []string
	for i, arg := range l.args {
		if arg == "--env" {
			envs = append(envs, l.args[i+1])
		}
	}
	return envs
}

func TestConnectionsAreMountedAndNeverPassedAsEnvironment(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Apply)

	document, _ := os.ReadFile(filepath.Join(l.controller.SecretDir, "controller-connections.json"))
	if string(l.mounted["/run/secrets/controller-connections.json"]) != string(document) {
		t.Errorf("the mounted document is not the rendered one")
	}
	for _, env := range l.envs() {
		name, _, found := strings.Cut(env, "=")
		if !found || !slices.Contains(launchVariables, name) {
			t.Errorf("--env %s is not one of the launcher's own", env)
		}
	}
	joined := strings.Join(l.args, "\n")
	for _, absent := range []string{sentinel, reference, "--env-file"} {
		if strings.Contains(joined, absent) {
			t.Errorf("the launch carries %q", absent)
		}
	}
	if l.args[len(l.args)-1] != "--apply" || l.args[len(l.args)-2] != running {
		t.Errorf("the launch ends %v", l.args[len(l.args)-2:])
	}
	if runs, _ := filepath.Glob(filepath.Join(l.controller.SecretDir, "run.*")); len(runs) != 0 {
		t.Errorf("the run's staging was left: %v", runs)
	}
}

func TestTheDocumentIsCopiedUnderTheSharedLock(t *testing.T) {
	l := newLaunch(t)
	checked := 0
	l.controller.Connections = func() error {
		checked++
		if checked == 2 && !l.locked {
			t.Error("the document was checked for the copy outside the lock")
		}
		return nil
	}

	l.run(t, Plan)

	if strings.Join(l.order, " ") != "lock unlock" {
		t.Errorf("lock order %v", l.order)
	}
	if checked != 2 {
		t.Errorf("the document was checked %d times, not before the run and again under the lock", checked)
	}
}

func TestNothingOfHQsOwnIsGivenToTheController(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Apply)

	joined := strings.Join(l.args, "\n")
	for _, absent := range []string{"target=/data", hostcore.AppEnvName, "sentinel-application-secret", "doorbell", "source=" + hostcore.RunDir + ",", "docker.sock"} {
		if strings.Contains(joined, absent) {
			t.Errorf("the controller is given %q", absent)
		}
	}
	for _, volume := range Volumes {
		if volume.Target != filepath.Dir(BridgeSocket) && strings.Contains(joined, volume.Name) {
			t.Errorf("the controller is given HQ's volume %s", volume.Name)
		}
	}
}

func TestTheBridgeIsTheWebContainersOwnReadOnly(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Apply)

	var bridge []string
	for _, arg := range l.args {
		if strings.Contains(arg, "hq-bridge") || strings.Contains(arg, "_bridge") || strings.Contains(arg, "BRIDGE") {
			bridge = append(bridge, arg)
		}
	}
	want := []string{
		"type=volume,source=severino-hq_severino_bridge,target=/run/hq-bridge,readonly",
		"SEVERINO_BRIDGE_SOCKET=" + BridgeSocket,
	}
	if !slices.Equal(bridge, want) {
		t.Errorf("bridge arguments %v", bridge)
	}
	// The web container is told the same socket on the same volume.
	web := strings.Join(Web{Image: running}.Args(), " ")
	if !strings.Contains(web, "SEVERINO_BRIDGE_SOCKET="+BridgeSocket) || !strings.Contains(web, "source=severino-hq_severino_bridge,target=/run/hq-bridge") {
		t.Error("the web container serves the bridge somewhere else")
	}
}

// The unit socket's directory reaches the controller's container and never
// the web container, which faces the network and shares the account.
func TestTheUnitSocketIsLentToTheControllerAlone(t *testing.T) {
	l := newLaunch(t)
	dir := filepath.Dir(l.controller.UnitSocket)
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}

	l.run(t, Apply)

	joined := strings.Join(l.args, "\n")
	if !strings.Contains(joined, "type=bind,source="+dir+",target="+UnitSocketDir+"\n") {
		t.Errorf("the controller is not lent the unit socket's directory: %v", l.args)
	}
	if !slices.Contains(l.envs(), "SEVERINO_UNIT_SOCKET="+UnitSocket) {
		t.Errorf("the controller is not told where the unit socket is: %v", l.envs())
	}
	web := strings.Join(Web{Image: running}.Args(), " ")
	if strings.Contains(web, UnitSocketDir) || strings.Contains(web, "SEVERINO_UNIT_SOCKET") {
		t.Errorf("the web container reaches the unit socket: %s", web)
	}
}

// A host whose socket unit is not installed yet lends nothing, since a bind
// of a missing source would stop the pass.
func TestNoUnitSocketIsLentWhereThereIsNone(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Apply)

	if joined := strings.Join(l.args, "\n"); strings.Contains(joined, "SEVERINO_UNIT_SOCKET") || strings.Contains(joined, UnitSocketDir) {
		t.Errorf("a missing unit socket is lent: %v", l.args)
	}
}

func TestAnUnsafeDocumentStopsTheLaunch(t *testing.T) {
	l := newLaunch(t)
	l.controller.Connections = func() error { return errors.New("controller secret directory must be on tmpfs") }

	if err := l.controller.Run(context.Background(), Apply); err == nil {
		t.Fatal("launched on a refused document")
	}
	if l.args != nil {
		t.Error("docker run was called")
	}
}

func TestReadingsAreNamedWhetherOrNotTheyCouldBeTaken(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Plan)

	envs := l.envs()
	for _, want := range []string{
		"SEVERINO_RENDER_STATUS=hq=/run/severino-hq/render-status/hq.json",
		"SEVERINO_HOST_UNITS=/run/severino-hq/units",
		"SEVERINO_HOST_RESTARTING_UNITS=/run/severino-hq/units-restarting",
		"SEVERINO_TAILNET_LOCK=/run/severino-hq/tailnet-lock.json",
		"SEVERINO_HOST_FILESYSTEMS=/run/severino-hq/filesystems.json",
		"SEVERINO_HOST_REBOOT=/run/severino-hq/reboot.json",
	} {
		if !slices.Contains(envs, want) {
			t.Errorf("%s is not named: %v", want, envs)
		}
	}
	for target := range l.mounted {
		if strings.HasPrefix(target, "/run/severino-hq/render-status/") || slices.Contains([]string{
			"/run/severino-hq/units", "/run/severino-hq/units-restarting",
			"/run/severino-hq/tailnet-lock.json", "/run/severino-hq/filesystems.json", "/run/severino-hq/reboot.json",
		}, target) {
			t.Errorf("%s was mounted with nothing to mount", target)
		}
	}
	for _, env := range envs {
		if strings.HasPrefix(env, "SEVERINO_TAILNET_STATUS") || strings.HasPrefix(env, "SEVERINO_HOST_FIREWALL") || strings.HasPrefix(env, "HQ_CONTROLLER_CA_FILE") {
			t.Errorf("%s named with nothing read", env)
		}
	}
}

// Root asks each real filesystem its space and hands the container the
// answer, never the mount table it read it from.
func TestEachFilesystemsSpaceIsMountedAsAReading(t *testing.T) {
	l := newLaunch(t)
	table := "/dev/sda2 / ext4 rw,relatime,errors=remount-ro 0 0\noverlay /var/lib/docker/overlay2/example/merged overlay rw 0 0\n"
	if err := os.WriteFile(l.controller.MountTable, []byte(table), 0o644); err != nil {
		t.Fatal(err)
	}

	l.run(t, Plan)

	var reading hostcore.FilesystemReading
	if err := json.Unmarshal(l.mounted["/run/severino-hq/filesystems.json"], &reading); err != nil {
		t.Fatalf("no filesystem reading was mounted: %v", err)
	}
	want := []hostcore.Filesystem{{Mount: "/", FreeBytes: 59 << 20, TotalBytes: 29 << 30}}
	if !slices.Equal(reading.Filesystems, want) || reading.ReadAt == "" {
		t.Errorf("mounted %+v, want %+v", reading, want)
	}
	if joined := strings.Join(l.args, "\n"); strings.Contains(joined, "/dev/sda2") || strings.Contains(joined, "source="+l.controller.MountTable) {
		t.Errorf("the mount table reached the container: %v", l.args)
	}
}

// Root reads the restart flag and asks logind, and hands the container what
// they said, never the flag's directory or the bus it asked on.
func TestAWaitingRestartIsMountedAsAReading(t *testing.T) {
	l := newLaunch(t)
	if err := os.WriteFile(l.controller.RebootRequired, []byte("*** System restart required ***\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(l.controller.RebootRequired+".pkgs", []byte("linux-image-example\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	faked := l.controller.Command
	l.controller.Command = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		if name == "busctl" {
			return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' '{"type":"(st)","data":["reboot",1893466800000000]}'`)
		}
		return faked(ctx, name, args...)
	}

	l.run(t, Plan)

	var reading hostcore.RebootReading
	if err := json.Unmarshal(l.mounted["/run/severino-hq/reboot.json"], &reading); err != nil {
		t.Fatalf("no restart reading was mounted: %v", err)
	}
	if !reading.Pending || reading.Since != "2029-12-31T18:00:00Z" || !slices.Equal(reading.Packages, []string{"linux-image-example"}) ||
		reading.ScheduledAction != "reboot" || reading.ScheduledAt != "2030-01-01T03:00:00Z" {
		t.Errorf("mounted %+v", reading)
	}
	if joined := strings.Join(l.args, "\n"); strings.Contains(joined, "source="+l.controller.RebootRequired) || strings.Contains(joined, "dbus") {
		t.Errorf("the flag or the bus reached the container: %v", l.args)
	}
}

func TestTheRenderersStatusIsMountedAsACopy(t *testing.T) {
	l := newLaunch(t)
	status := filepath.Join(l.controller.SecretDir, "status.json")
	if err := os.WriteFile(status, []byte(`{"schema_version":1}`), 0o644); err != nil {
		t.Fatal(err)
	}

	l.run(t, Plan)

	if string(l.mounted["/run/severino-hq/render-status/hq.json"]) != `{"schema_version":1}` {
		t.Errorf("mounted %q", l.mounted["/run/severino-hq/render-status/hq.json"])
	}
	for _, arg := range l.args {
		if strings.Contains(arg, "source="+status) || strings.Contains(arg, "source="+l.controller.SecretDir+",") {
			t.Errorf("the document or its directory was mounted: %s", arg)
		}
	}
}

func TestALinkedStatusIsNotFollowed(t *testing.T) {
	l := newLaunch(t)
	if err := os.Symlink(filepath.Join(l.controller.SecretDir, "controller-connections.json"), filepath.Join(l.controller.SecretDir, "status.json")); err != nil {
		t.Fatal(err)
	}

	l.run(t, Plan)

	if _, ok := l.mounted["/run/severino-hq/render-status/hq.json"]; ok {
		t.Error("a linked status was mounted")
	}
	if strings.Contains(strings.Join(l.args, "\n"), sentinel) {
		t.Error("the link's target reached the launch")
	}
}

func TestHostRootsAndTheOpCLIAreLent(t *testing.T) {
	l := newLaunch(t)
	if err := os.MkdirAll(l.controller.CertStore, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(l.controller.CertStore, "example.crt"), []byte("ROOT\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	l.controller.LookPath = func(string) (string, error) { return "/usr/bin/op", nil }

	l.run(t, Plan)

	if string(l.mounted["/run/secrets/severino_controller_ca.pem"]) != "ROOT\n" {
		t.Error("the host's roots were not lent")
	}
	if !slices.Contains(l.args, "type=bind,source=/usr/bin/op,target=/usr/bin/op,readonly") {
		t.Error("op was not lent read-only")
	}
}

func TestReportedIsEveryUnitWithAStateOfItsOwn(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "a-.service.d"), 0o755); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"a.service":                   "",
		"job@.service":                "",
		"a.timer":                     "[Timer]\nUnit=job@one.service\n",
		"b.timer":                     "[Timer]\nUnit=other@one.service\n",
		"a.path":                      "[Path]\nUnit=a.service\n",
		"a-.service.d/10-failed.conf": "[Unit]\nOnFailure=job@two.service  job@.service job@$(id).service a.service\n",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	shipped, _ := Shipped(dir)

	reported, err := Reported(dir, shipped)

	if err != nil {
		t.Fatal(err)
	}
	want := []string{"a.path", "a.service", "a.timer", "b.timer", "job@one.service", "job@two.service"}
	if !slices.Equal(reported, want) {
		t.Errorf("reported %v", reported)
	}
}

func TestNoPropertyAskedCarriesWhatAUnitRunsOrIsGiven(t *testing.T) {
	for _, property := range UnitProperties {
		switch property {
		case "ExecMainCode", "ExecMainStatus", "UnitFileState":
			continue
		}
		if regexp.MustCompile(`Exec|Environment|Credential|Path|Director|File|Passphrase`).MatchString(property) {
			t.Errorf("asks for %s", property)
		}
	}
}

func TestTheFirewallIsDistilled(t *testing.T) {
	bound := []byte("chain input {\n\tiifname \"tailscale0\" tcp dport 8000 accept\n\tiifname != \"tailscale0\" tcp dport 8000 drop\n}\n")
	open := []byte("chain input {\n\ttcp dport 8000 accept\n}\n")
	at := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

	for name, chain := range map[string][]byte{"bound": bound, "open": open} {
		data, err := ReadFirewall(chain, at)
		if err != nil {
			t.Fatal(err)
		}
		var reading Firewall
		if err := json.Unmarshal(data, &reading); err != nil {
			t.Fatal(err)
		}
		if reading.AcceptRequiresInterface != (name == "bound") || reading.ForeignInterfaceDropped != (name == "bound") {
			t.Errorf("%s: %+v", name, reading)
		}
		if reading.ReadAt != "2026-01-02T03:04:05Z" || reading.Record != "interface-binding" {
			t.Errorf("%s: %+v", name, reading)
		}
	}
}

// Whatever nft prints, the reading is the one record of two answers and
// when they were read: nothing of the chain's own text reaches HQ.
func FuzzReadFirewall(f *testing.F) {
	f.Add([]byte("chain input {\n\tiifname \"tailscale0\" tcp dport 8000 accept\n\tiifname != \"tailscale0\" tcp dport 8000 drop\n}\n"))
	f.Add([]byte("chain input {\n\ttcp dport 8000 accept\n}\n"))
	f.Add([]byte("chain input {\n\tiifname != \"tailscale0\" drop comment \"\\\"},{\\\"x\\\":1\"\n}\n"))
	at := time.Date(2026, 1, 2, 3, 4, 5, 0, time.FixedZone("example", -3600))
	f.Fuzz(func(t *testing.T, chain []byte) {
		data, err := ReadFirewall(chain, at)
		if err != nil {
			t.Fatalf("the reading failed: %v", err)
		}
		var reading Firewall
		if err := jsonv2.Unmarshal(data, &reading, jsonv2.RejectUnknownMembers(true)); err != nil {
			t.Fatalf("read %s (%v)", data, err)
		}
		fixed := Firewall{Record: "interface-binding", Interface: "tailscale0", ReadAt: "2026-01-02T04:04:05Z",
			AcceptRequiresInterface: reading.AcceptRequiresInterface, ForeignInterfaceDropped: reading.ForeignInterfaceDropped}
		if want, _ := json.Marshal(fixed); !bytes.Equal(data, want) {
			t.Fatalf("read %s, not one of the fixed records", data)
		}
	})
}

func TestTheCertificateStateIsTheControllersWithoutFollowingLinks(t *testing.T) {
	l := newLaunch(t)
	if err := os.MkdirAll(filepath.Join(l.controller.ACME, "live", "example"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(l.controller.ACME, "live", "example", "privkey.pem"), []byte("key"), 0o600); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(l.controller.ACME, "escape")); err != nil {
		t.Fatal(err)
	}
	var owned []string
	l.controller.Owner = func(root *os.Root, name string) error {
		if root.Name() == l.controller.ACME {
			owned = append(owned, name)
		}
		return nil
	}

	l.run(t, Plan)

	slices.Sort(owned)
	want := []string{".", "escape", "live", "live/example", "live/example/privkey.pem"}
	if !slices.Equal(owned, want) {
		t.Errorf("owned %v", owned)
	}
}

// glanceDocument is a rendered document with a connection of each kind a
// glance reads, the listener's, and one neither reads.
func glanceDocument(t *testing.T, l *launch) {
	t.Helper()
	store := connections.Store{Vault: "Example", Item: "example"}
	document, err := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{
		{Ref: "example-portainer", Provider: "portainer", Store: store,
			APIToken: &connections.APIToken{URL: "https://portainer.example.com", APIToken: "portainer-token"}},
		{Ref: "example-ssh", Provider: "ssh", Store: store,
			SSHTransport: &connections.SSHTransport{Host: "192.0.2.10", User: "example", HostKey: "key", Identity: "example-ssh", Port: "22"}},
		{Ref: "example-dns", Provider: "cloudflare_dns", Store: store,
			APIToken: &connections.APIToken{URL: "https://api.example.com", APIToken: sentinel}},
		{Ref: "example-events", Provider: string(runtime.ConnectionProviderCloudflareEvents), Store: store,
			APIToken: &connections.APIToken{URL: "wss://events.example.com/socket", APIToken: "socket-token"}},
	}}.Encode()
	if err != nil {
		t.Fatal(err)
	}
	rendered := filepath.Join(l.controller.SecretDir, "controller-connections.json")
	if err := os.Remove(rendered); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(rendered, document, 0o400); err != nil {
		t.Fatal(err)
	}
}

// A glance pass is given the connections it reads a machine through and the
// identities they name, the listener the events connection alone, and
// neither any of the rest.
func TestAPartialPassIsGivenOnlyTheConnectionsItReads(t *testing.T) {
	for _, c := range []struct {
		pass       Pass
		refs       []string
		role, last string
	}{
		{Glance, []string{"example-portainer", "example-ssh"}, "glance", "--glance"},
		{Listen, []string{"example-events"}, "listener", "listen"},
	} {
		t.Run(c.role, func(t *testing.T) {
			l := newLaunch(t)
			glanceDocument(t, l)

			l.run(t, c.pass)

			document, err := connections.Decode(l.mounted[controllerConnections])
			if err != nil {
				t.Fatal(err)
			}
			var refs []string
			for _, connection := range document.Connections {
				refs = append(refs, connection.Ref)
			}
			if !slices.Equal(refs, c.refs) {
				t.Errorf("the pass is given %v", refs)
			}
			if strings.Contains(string(l.mounted[controllerConnections]), sentinel) {
				t.Error("the pass is given a credential it does not read")
			}
			if l.args[len(l.args)-1] != c.last || l.args[len(l.args)-2] != running {
				t.Errorf("the launch ends %v", l.args[len(l.args)-2:])
			}
			if !slices.Contains(l.args, "severino-hq.role="+c.role) {
				t.Errorf("the pass's container is not labeled as one: %v", l.args)
			}
		})
	}
}

// The listener faces the internet, so its container reaches only public
// addresses, on a network its table confines, and HQ only through the
// socket that serves the events action alone.
func TestTheListenerReachesOnlyPublicAddressesAndTheEventsBridge(t *testing.T) {
	l := newLaunch(t)
	glanceDocument(t, l)

	l.run(t, Listen)

	if !l.loaded {
		t.Error("the listener's egress table was not loaded")
	}
	if network := l.args[slices.Index(l.args, "--network")+1]; network != ListenerNetwork {
		t.Errorf("the listener runs on %s", network)
	}
	for i, arg := range l.args {
		if arg == "--dns" && !slices.Contains(PublicResolvers, l.args[i+1]) {
			t.Errorf("the listener resolves with %s", l.args[i+1])
		}
	}
	if slices.ContainsFunc(l.args, func(arg string) bool { return strings.Contains(arg, filepath.Dir(BridgeSocket)+",") }) {
		t.Errorf("the listener is lent the controller's bridge: %v", l.args)
	}
}

// With no events connection there is no socket to hold, and no container.
func TestAListenerWithNoEventsConnectionStartsNothing(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Listen)

	if l.args != nil {
		t.Errorf("a listener with nothing to hold started %v", l.args)
	}
}

// The automation worker's connection, and its identity, never reach a
// controller or glance pass, and a pass holds only the identities of the
// connections it is given.
func TestNoPassHoldsTheAutomationWorkersKey(t *testing.T) {
	for _, pass := range []Pass{Plan, Apply, Glance, Listen} {
		t.Run(fmt.Sprint(pass), func(t *testing.T) {
			l := newLaunch(t)
			glanceDocument(t, l)
			withRunnerConnection(t, l.controller.SecretDir)

			l.run(t, pass)

			document, err := connections.Decode(l.mounted[controllerConnections])
			if err != nil {
				t.Fatal(err)
			}
			for _, connection := range document.Connections {
				if connection.Ref == "example-machine" {
					t.Errorf("the pass is given %s", connection)
				}
			}
			slices.Sort(l.lentFiles)
			want := map[Pass][]string{
				Glance: {"example-ssh", "example-ssh.pub", "known_hosts"},
				Listen: {"known_hosts"},
			}[pass]
			if !pass.partial() {
				want = []string{"example-dns.key", "example-ssh", "example-ssh.pub", "known_hosts"}
			}
			if !slices.Equal(l.lentFiles, want) {
				t.Errorf("the pass holds %v, want %v", l.lentFiles, want)
			}
		})
	}
}

func TestThePinnedHostKeysAreTheRenderersFile(t *testing.T) {
	if knownHosts != project.KnownHosts {
		t.Errorf("a run is lent %s, the renderer writes %s", knownHosts, project.KnownHosts)
	}
}

// withRunnerConnection adds the automation worker's connection to the
// rendered document, and the files the renderer writes for the document's
// connections: each SSH identity, a signing key, the pinned host keys.
func withRunnerConnection(t *testing.T, secrets string) {
	t.Helper()
	rendered := filepath.Join(secrets, "controller-connections.json")
	data, err := os.ReadFile(rendered)
	if err != nil {
		t.Fatal(err)
	}
	document, err := connections.Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	document.Connections = append(document.Connections, connections.Connection{
		Ref: "example-machine", Provider: "ssh", Store: connections.Store{Vault: "Example", Item: "example-machine"},
		SSHTransport: &connections.SSHTransport{Host: "192.0.2.20", User: "example-read", HostKey: "key", Identity: "example-machine", Port: "22", Role: "runner",
			Machine: "example-machine", Profile: connections.ReadProfile},
	})
	if data, err = document.Encode(); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(rendered); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(rendered, data, 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(filepath.Join(secrets, "ssh")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(secrets, "ssh"), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"example-machine", "example-machine.pub", "example-ssh", "example-ssh.pub", "example-dns.key", "known_hosts"} {
		if err := os.WriteFile(filepath.Join(secrets, "ssh", name), []byte(name+"\n"), 0o400); err != nil {
			t.Fatal(err)
		}
	}
}

// A glance or listener pass is given less than a controller pass: no
// certificate state, unit socket, reading of this host, delivery or way to
// hand root work.
func TestAPartialPassIsGivenNothingElseOfTheControllers(t *testing.T) {
	for _, pass := range []Pass{Glance, Listen} {
		t.Run(fmt.Sprint(pass), func(t *testing.T) {
			l := newLaunch(t)
			glanceDocument(t, l)
			for _, dir := range []string{filepath.Dir(l.controller.UnitSocket), l.controller.CertStore} {
				if err := os.MkdirAll(dir, 0o700); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.WriteFile(filepath.Join(l.controller.CertStore, "example.crt"), []byte("ROOT\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			l.controller.LookPath = func(string) (string, error) { return "/usr/bin/op", nil }

			l.run(t, pass)

			var names []string
			for _, env := range l.envs() {
				name, _, _ := strings.Cut(env, "=")
				names = append(names, name)
			}
			slices.Sort(names)
			bridge, variable := BridgeSocket, "SEVERINO_BRIDGE_SOCKET"
			if pass == Listen {
				bridge, variable = EventsBridgeSocket, "SEVERINO_EVENTS_BRIDGE_SOCKET"
			}
			want := []string{"HQ_CONTROLLER_CA_FILE", "HQ_CONTROLLER_CONNECTIONS", "HQ_CONTROLLER_RUNS", "HQ_CONTROLLER_SSH_DIR", variable}
			if !slices.Equal(names, want) {
				t.Errorf("the pass is told %v, want %v", names, want)
			}
			var targets []string
			for i, arg := range l.args {
				if arg == "--mount" {
					_, target, _ := strings.Cut(l.args[i+1], ",target=")
					target, _, _ = strings.Cut(target, ",")
					targets = append(targets, target)
				}
			}
			slices.Sort(targets)
			if want := []string{filepath.Dir(bridge), controllerConnections, controllerSSH, controllerCA, RunsDir}; !slices.Equal(targets, slices.Sorted(slices.Values(want))) {
				t.Errorf("the pass mounts %v", targets)
			}
			if string(l.mounted[controllerCA]) != "ROOT\n" {
				t.Error("the host's roots were not lent to the pass")
			}
			if _, err := os.Stat(l.controller.ACME); err == nil {
				t.Error("the pass made the certificate state")
			}
		})
	}
}
