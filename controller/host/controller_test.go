package host

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"
)

// Values that may reach the container only inside the mounted document.
const (
	sentinel  = "sentinel-provider-token-value"
	reference = "sentinel-connection"
)

// launchVariables is every variable the launcher may set: a path, a name, an
// image or a nonce.
var launchVariables = []string{
	"HQ_CONTROLLER_RUN", "SEVERINO_BRIDGE_SOCKET", "HQ_CONTROLLER_CONNECTIONS",
	"HQ_CONTROLLER_SSH_DIR", "HQ_ACME_DIR", "HQ_CONTROLLER_IMAGE", "SEVERINO_HQ_SOURCE_REPOSITORY",
	"HQ_CONTROLLER_CA_FILE", "SEVERINO_TAILNET_STATUS", "SEVERINO_TAILNET_LOCK",
	"SEVERINO_HOST_FIREWALL", "SEVERINO_RENDER_STATUS", "SEVERINO_HOST_UNITS",
}

type launch struct {
	f          *fakeHost
	controller *Controller
	args       []string
	mounted    map[string][]byte
	locked     bool
	order      []string
}

func newLaunch(t *testing.T) *launch {
	t.Helper()
	f := newFakeHost(t)
	f.running = running
	secrets := filepath.Join(f.root, "secrets")
	os.MkdirAll(filepath.Join(secrets, "ssh"), 0o700)
	os.WriteFile(filepath.Join(secrets, "ssh", reference), []byte("an identity\n"), 0o600)
	document, _ := json.Marshal(map[string]any{"schema_version": 2, "connections": []any{
		map[string]any{"ref": reference, "api_token": map[string]string{"api_token": sentinel}},
	}})
	os.WriteFile(filepath.Join(secrets, "controller-connections.json"), document, 0o400)
	os.MkdirAll(filepath.Join(secrets, "web"), 0o700)
	os.WriteFile(filepath.Join(secrets, "web", AppEnvName), []byte("DJANGO_SECRET_KEY=sentinel-application-secret\n"), 0o400)

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
				}
			}
		}
		if name == "nft" || name == "systemctl" {
			return exec.CommandContext(ctx, "/bin/sh", "-c", "exit 1")
		}
		return program(ctx, name, args...)
	}
	l.controller = &Controller{
		Machine:     f.machine,
		SecretDir:   secrets,
		ACME:        filepath.Join(f.root, "acme"),
		CertStore:   filepath.Join(f.root, "certs"),
		Tailscaled:  filepath.Join(f.root, "absent.sock"),
		Connections: func() error { return nil },
		Lock: func(context.Context) (func(), error) {
			l.locked = true
			l.order = append(l.order, "lock")
			return func() { l.locked = false; l.order = append(l.order, "unlock") }, nil
		},
		Owner:    func(*os.Root, string) error { return nil },
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

func (l *launch) run(t *testing.T, apply bool) {
	t.Helper()
	if err := l.controller.Run(context.Background(), apply); err != nil {
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

	l.run(t, true)

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

	l.run(t, false)

	if strings.Join(l.order, " ") != "lock unlock" {
		t.Errorf("lock order %v", l.order)
	}
	if checked != 2 {
		t.Errorf("the document was checked %d times, not before the run and again under the lock", checked)
	}
}

func TestNothingOfHQsOwnIsGivenToTheController(t *testing.T) {
	l := newLaunch(t)

	l.run(t, true)

	joined := strings.Join(l.args, "\n")
	for _, absent := range []string{"target=/data", AppEnvName, "sentinel-application-secret", "doorbell", "source=" + RunDir + ",", "docker.sock"} {
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

	l.run(t, true)

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

func TestAnUnsafeDocumentStopsTheLaunch(t *testing.T) {
	l := newLaunch(t)
	l.controller.Connections = func() error { return errors.New("Controller secret directory must be on tmpfs.") }

	if err := l.controller.Run(context.Background(), true); err == nil {
		t.Fatal("launched on a refused document")
	}
	if l.args != nil {
		t.Error("docker run was called")
	}
}

func TestReadingsAreNamedWhetherOrNotTheyCouldBeTaken(t *testing.T) {
	l := newLaunch(t)

	l.run(t, false)

	envs := l.envs()
	for _, want := range []string{
		"SEVERINO_RENDER_STATUS=hq=/run/severino-hq/render-status/hq.json",
		"SEVERINO_HOST_UNITS=/run/severino-hq/units",
	} {
		if !slices.Contains(envs, want) {
			t.Errorf("%s is not named: %v", want, envs)
		}
	}
	for target := range l.mounted {
		if strings.HasPrefix(target, "/run/severino-hq/render-status/") || target == "/run/severino-hq/units" {
			t.Errorf("%s was mounted with nothing to mount", target)
		}
	}
	for _, env := range envs {
		if strings.HasPrefix(env, "SEVERINO_TAILNET") || strings.HasPrefix(env, "SEVERINO_HOST_FIREWALL") || strings.HasPrefix(env, "HQ_CONTROLLER_CA_FILE") {
			t.Errorf("%s named with nothing read", env)
		}
	}
}

func TestTheRenderersStatusIsMountedAsACopy(t *testing.T) {
	l := newLaunch(t)
	status := filepath.Join(l.controller.SecretDir, "status.json")
	os.WriteFile(status, []byte(`{"schema_version":1}`), 0o644)

	l.run(t, false)

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
	os.Symlink(filepath.Join(l.controller.SecretDir, "controller-connections.json"), filepath.Join(l.controller.SecretDir, "status.json"))

	l.run(t, false)

	if _, ok := l.mounted["/run/severino-hq/render-status/hq.json"]; ok {
		t.Error("a linked status was mounted")
	}
	if strings.Contains(strings.Join(l.args, "\n"), sentinel) {
		t.Error("the link's target reached the launch")
	}
}

func TestHostRootsAndTheOpCLIAreLent(t *testing.T) {
	l := newLaunch(t)
	os.MkdirAll(l.controller.CertStore, 0o755)
	os.WriteFile(filepath.Join(l.controller.CertStore, "example.crt"), []byte("ROOT\n"), 0o644)
	l.controller.LookPath = func(string) (string, error) { return "/usr/bin/op", nil }

	l.run(t, false)

	if string(l.mounted["/run/secrets/severino_controller_ca.pem"]) != "ROOT\n" {
		t.Error("the host's roots were not lent")
	}
	if !slices.Contains(l.args, "type=bind,source=/usr/bin/op,target=/usr/bin/op,readonly") {
		t.Error("op was not lent read-only")
	}
}

func TestReportedIsEveryUnitWithAStateOfItsOwn(t *testing.T) {
	dir := t.TempDir()
	os.MkdirAll(filepath.Join(dir, "a-.service.d"), 0o755)
	files := map[string]string{
		"a.service":                   "",
		"job@.service":                "",
		"a.timer":                     "[Timer]\nUnit=job@one.service\n",
		"b.timer":                     "[Timer]\nUnit=other@one.service\n",
		"a.path":                      "[Path]\nUnit=a.service\n",
		"a-.service.d/10-failed.conf": "[Unit]\nOnFailure=job@two.service  job@.service job@$(id).service a.service\n",
	}
	for name, body := range files {
		os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644)
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
		json.Unmarshal(data, &reading)
		if reading.AcceptRequiresInterface != (name == "bound") || reading.ForeignInterfaceDropped != (name == "bound") {
			t.Errorf("%s: %+v", name, reading)
		}
		if reading.ReadAt != "2026-01-02T03:04:05Z" || reading.Record != "interface-binding" {
			t.Errorf("%s: %+v", name, reading)
		}
	}
}

func TestTheCertificateStateIsTheControllersWithoutFollowingLinks(t *testing.T) {
	l := newLaunch(t)
	os.MkdirAll(filepath.Join(l.controller.ACME, "live", "example"), 0o700)
	os.WriteFile(filepath.Join(l.controller.ACME, "live", "example", "privkey.pem"), []byte("key"), 0o600)
	outside := t.TempDir()
	os.Symlink(outside, filepath.Join(l.controller.ACME, "escape"))
	var owned []string
	l.controller.Owner = func(root *os.Root, name string) error {
		if root.Name() == l.controller.ACME {
			owned = append(owned, name)
		}
		return nil
	}

	l.run(t, false)

	slices.Sort(owned)
	want := []string{".", "escape", "live", "live/example", "live/example/privkey.pem"}
	if !slices.Equal(owned, want) {
		t.Errorf("owned %v", owned)
	}
}
