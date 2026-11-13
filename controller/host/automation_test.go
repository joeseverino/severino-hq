package host

import (
	"bytes"
	"context"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
)

const automationImageRef = "ghcr.io/example/automation@sha256:" + "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

const automationNetworkJSON = `{"Name":"severino-hq-automation","Id":"4567cdef","Scope":"local","Driver":"bridge","EnableIPv4":true,"EnableIPv6":false,` +
	`"IPAM":{"Driver":"default","Options":null,"Config":[{"Subnet":"10.231.1.0/24","Gateway":"10.231.1.1"}]},"Internal":false,"Attachable":false,` +
	`"Containers":{},"Options":{"com.docker.network.bridge.name":"hq-automation0"},"Labels":{}}`

// worker is one launch of the automation worker against a fake docker and
// nft: every call made, what nft was given, and what the container found
// lent to it while it ran.
type worker struct {
	a       *Automation
	root    string
	grant   Grant
	docker  string
	mu      sync.Mutex
	calls   [][]string
	lent    map[string][]byte
	owned   []string
	stderr  bytes.Buffer
	running func(args []string)
}

func newWorker(t *testing.T) *worker {
	t.Helper()
	root := t.TempDir()
	w := &worker{root: root, docker: "exit 0", lent: map[string][]byte{}}
	secrets := filepath.Join(root, "secrets")
	for _, dir := range []string{filepath.Join(secrets, "ssh"), filepath.Join(root, "journal"), filepath.Join(root, "lib", "deploy", "automation"), filepath.Join(root, "grant")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	write := func(path, body string) {
		if err := os.WriteFile(path, []byte(body), 0o400); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join(root, "lib", AutomationImage), automationImageRef+"\n")
	store := connections.Store{Vault: "Example", Item: "example"}
	transport := func(host, port, role string) *connections.SSHTransport {
		return &connections.SSHTransport{Host: host, Port: port, User: "example-read", HostKey: "ssh-ed25519 AAAA", Identity: "key", Role: role}
	}
	document, err := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{
		{Ref: "example-server", Provider: "ssh", Store: store, SSHTransport: transport("192.0.2.10", "22", connections.RunnerRole)},
		{Ref: "example-edge", Provider: "ssh", Store: store, SSHTransport: transport("198.51.100.7", "7722", connections.RunnerRole)},
		{Ref: "example-named", Provider: "ssh", Store: store, SSHTransport: transport("named.example.com", "22", connections.RunnerRole)},
		{Ref: "example-controller", Provider: "ssh", Store: store, SSHTransport: transport("198.51.100.7", "7722", "")},
		{Ref: "example-dns", Provider: "cloudflare_dns", Store: store, APIToken: &connections.APIToken{URL: "https://api.example.com", APIToken: sentinel}},
	}}.Encode()
	if err != nil {
		t.Fatal(err)
	}
	write(filepath.Join(secrets, "controller-connections.json"), string(document))
	for _, name := range []string{"example-server", "example-server.pub", "example-edge", "example-edge.pub", "example-named", "example-named.pub",
		"example-controller", "example-controller.pub", "example-dns.key", "known_hosts"} {
		write(filepath.Join(secrets, "ssh", name), name+"\n")
	}
	w.grant = Grant{Name: "automation-projects", Repositories: []string{"example/playbooks"},
		Permissions: map[string]string{"contents": "read"}, Path: filepath.Join(root, "grant", "github_token")}
	write(w.grant.Path, "example-token-one")

	machine := &Machine{Lib: filepath.Join(root, "lib")}
	machine.Owner = func(_ *os.Root, name string) error {
		w.mu.Lock()
		defer w.mu.Unlock()
		w.owned = append(w.owned, name)
		return nil
	}
	machine.Command = w.command
	w.a = &Automation{
		Machine:     machine,
		SecretDir:   secrets,
		Journal:     filepath.Join(root, "journal"),
		Project:     &AutomationConfig{Repository: "example/playbooks", Directory: "ansible", Grant: w.grant.Name},
		Grants:      []Grant{w.grant},
		ID:          "example-host",
		Bound:       time.Hour,
		TokenPoll:   time.Hour,
		Connections: func() error { return nil },
		Lock:        func(context.Context) (func(), error) { return func() {}, nil },
		Stdout:      &bytes.Buffer{}, Stderr: &w.stderr,
	}
	return w
}

func (w *worker) command(ctx context.Context, name string, args ...string) *exec.Cmd {
	w.mu.Lock()
	w.calls = append(w.calls, append([]string{name}, args...))
	w.mu.Unlock()
	answer := func(out string, code int) *exec.Cmd {
		return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
	}
	switch {
	case name == "docker" && args[0] == "run":
		w.collect(args)
		if w.running != nil {
			w.running(args)
		}
		return exec.CommandContext(ctx, "/bin/sh", "-c", w.docker)
	case name == "docker" && slices.Equal(args[:2], []string{"network", "inspect"}):
		return answer(automationNetworkJSON, 0)
	case name == "nft":
		return exec.CommandContext(ctx, "/bin/sh", "-c", `cat > "$1"`, "sh", filepath.Join(w.root, "loaded.nft"))
	}
	return answer("", 0)
}

// collect keeps every lent file the container would find, by its path in
// the container: the run's staging is gone once the launch returns.
func (w *worker) collect(args []string) {
	for i, arg := range args {
		source, target, ok := bind(arg)
		if i == 0 || args[i-1] != "--mount" || !ok {
			continue
		}
		if data, err := os.ReadFile(source); err == nil {
			w.lent[target] = data
			continue
		}
		entries, _ := os.ReadDir(source)
		for _, entry := range entries {
			if data, err := os.ReadFile(filepath.Join(source, entry.Name())); err == nil {
				w.lent[target+"/"+entry.Name()] = data
			}
		}
	}
}

func (w *worker) run(t *testing.T) []string {
	t.Helper()
	if err := w.a.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, call := range w.calls {
		if runsContainer(call) {
			return call[1:]
		}
	}
	t.Fatalf("no container ran: %q", w.calls)
	return nil
}

// The worker holds the runner-role connections it can reach and their
// identities alone: not the controller's connection to the same machine,
// not a credential of another kind, not a signing key.
func TestTheWorkerIsLentOnlyItsOwnConnectionsAndKeys(t *testing.T) {
	w := newWorker(t)

	w.run(t)

	document, err := connections.Decode(w.lent[automationConnections])
	if err != nil {
		t.Fatal(err)
	}
	var refs []string
	for _, connection := range document.Connections {
		refs = append(refs, connection.Ref)
	}
	if !slices.Equal(refs, []string{"example-edge", "example-server"}) {
		t.Errorf("the worker is lent %v", refs)
	}
	var files []string
	for target := range w.lent {
		if name, ok := strings.CutPrefix(target, automationSSH+"/"); ok {
			files = append(files, name)
		}
	}
	slices.Sort(files)
	if want := []string{"example-edge", "example-edge.pub", "example-server", "example-server.pub", "known_hosts"}; !slices.Equal(files, want) {
		t.Errorf("the worker holds %v, want %v", files, want)
	}
	if string(w.lent[automationToken+"/"+automationTokenName]) != "example-token-one" {
		t.Error("the worker is not lent its grant's token")
	}
	for target, data := range w.lent {
		if strings.Contains(string(data), sentinel) {
			t.Errorf("%s holds a credential the worker does not use", target)
		}
	}
	if !strings.Contains(w.stderr.String(), "example-named") {
		t.Errorf("a runner connection the table cannot name is left out without a word: %q", w.stderr.String())
	}
	if entries, _ := os.ReadDir(w.a.SecretDir); slices.ContainsFunc(entries, func(e os.DirEntry) bool { return strings.HasPrefix(e.Name(), "automation.") }) {
		t.Error("the run's directory outlives the worker")
	}
}

// The worker's network is made and its table loaded, naming exactly the
// lent connections' destinations, before the container starts on it.
func TestTheWorkerReachesOnlyItsConnectionsBeyondPublicAddresses(t *testing.T) {
	w := newWorker(t)

	args := w.run(t)

	var order []string
	for _, call := range w.calls {
		order = append(order, call[0]+" "+call[1])
	}
	if !slices.Equal(order, []string{"docker network", "nft -f", "docker run"}) {
		t.Fatalf("ran %q", w.calls)
	}
	loaded, err := os.ReadFile(filepath.Join(w.root, "loaded.nft"))
	if err != nil {
		t.Fatal(err)
	}
	want := automationEgress
	want.reach = []netip.AddrPort{netip.MustParseAddrPort("192.0.2.10:22"), netip.MustParseAddrPort("198.51.100.7:7722")}
	if string(loaded) != want.load() {
		t.Errorf("nft was given\n%s", loaded)
	}
	if network := args[slices.Index(args, "--network")+1]; network != AutomationNetwork {
		t.Errorf("the worker runs on %s", network)
	}
}

// The table as nft is given it: a destination in reach is let through the
// forward hook and, being one of the host's own addresses, the input hook,
// where the connection takes the address it was sent to as its source, so a
// login to the host itself comes from the host.
func TestTheWorkersTableIsTheRenderedOne(t *testing.T) {
	confined := automationEgress
	confined.reach = []netip.AddrPort{netip.MustParseAddrPort("192.0.2.10:22"), netip.MustParseAddrPort("198.51.100.7:7722")}
	want := `table inet severino_hq_automation {
	chain forward {
		type filter hook forward priority filter - 10; policy accept;
		iifname "hq-automation0" meta nfproto ipv6 drop
		iifname "hq-automation0" ip saddr != 10.231.1.0/24 drop
		ip saddr 10.231.1.0/24 ip daddr . tcp dport { 192.0.2.10 . 22, 198.51.100.7 . 7722 } accept
		ip saddr 10.231.1.0/24 ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/3 } drop
	}

	chain input {
		type filter hook input priority filter - 10; policy accept;
		ip saddr 10.231.1.0/24 ip daddr . tcp dport { 192.0.2.10 . 22, 198.51.100.7 . 7722 } accept
		iifname "hq-automation0" drop
		ip saddr 10.231.1.0/24 drop
	}

	chain source {
		type nat hook input priority srcnat; policy accept;
		iifname "hq-automation0" ip saddr 10.231.1.0/24 snat ip to ip daddr . tcp dport map { 192.0.2.10 . 22 : 192.0.2.10, 198.51.100.7 . 7722 : 198.51.100.7 }
	}
}
`
	if got := confined.rules(); got != want {
		t.Errorf("rendered\n%s\nwant\n%s", got, want)
	}
	// The two networks never share addresses, so neither table names the
	// other's containers.
	if netip.MustParsePrefix(renovateEgress.subnet).Overlaps(netip.MustParsePrefix(automationEgress.subnet)) || renovateEgress.bridge == automationEgress.bridge || renovateEgress.table == automationEgress.table {
		t.Error("the worker's network is the Renovate run's")
	}
}

// The container: the web account the bridge admits, no capability, a
// read-only root, the lent files read-only, the journal the one host path
// it writes, and the worker's own program.
func TestTheWorkersContainerIsConfined(t *testing.T) {
	w := newWorker(t)

	args := w.run(t)

	for _, want := range [][]string{
		{"--user", fmt.Sprintf("%d:%d", hostcore.WebUID, hostcore.WebGID)},
		{"--cap-drop", "ALL"},
		{"--security-opt", "no-new-privileges:true"},
		{"--entrypoint", "python"},
	} {
		if i := slices.Index(args, want[0]); i < 0 || args[i+1] != want[1] {
			t.Errorf("the container is not run with %v: %v", want, args)
		}
	}
	if !slices.Contains(args, "--read-only") || !slices.Equal(args[len(args)-3:], []string{automationImageRef, "-m", "hq_runner"}) {
		t.Errorf("the container is not the worker's read-only program: %v", args)
	}
	for i, arg := range args {
		if i == 0 || args[i-1] != "--mount" {
			continue
		}
		if !strings.HasSuffix(arg, ",readonly") && arg != "type=bind,source="+w.a.Journal+",target="+automationJournal {
			t.Errorf("the container may write %s", arg)
		}
	}
	env := map[string]string{}
	for i, arg := range args {
		if arg == "--env" {
			name, value, _ := strings.Cut(args[i+1], "=")
			env[name] = value
		}
	}
	for name, value := range map[string]string{
		"HQ_AUTOMATION_REPOSITORY": "example/playbooks", "HQ_AUTOMATION_PROJECT_DIR": "ansible",
		"HQ_AUTOMATION_BRIDGE_SOCKET": BridgeSocket, "HQ_AUTOMATION_ID": "example-host",
		"HQ_AUTOMATION_TOKEN_FILE": automationToken + "/" + automationTokenName,
	} {
		if env[name] != value {
			t.Errorf("%s=%q, want %q", name, env[name], value)
		}
	}
}

// The variables the worker is told are the ones hq_runner reads, each one.
func TestTheWorkerIsToldWhatHqRunnerReads(t *testing.T) {
	source, err := os.ReadFile(filepath.Join("..", "..", "hq_runner", "__main__.py"))
	if err != nil {
		t.Fatal(err)
	}
	var read []string
	for line := range strings.SplitSeq(string(source), "\n") {
		if _, value, ok := strings.Cut(line, ` = "HQ_AUTOMATION_`); ok {
			read = append(read, "HQ_AUTOMATION_"+strings.TrimSuffix(strings.Fields(value)[0], `"`))
		}
	}
	args := AutomationArgs("example", automationImageRef, "/run", "/journal", "example-host",
		AutomationConfig{Repository: "example/playbooks", Directory: "ansible", Grant: "example"})
	var told []string
	for i, arg := range args {
		if arg == "--env" {
			name, _, _ := strings.Cut(args[i+1], "=")
			told = append(told, name)
		}
	}
	slices.Sort(read)
	slices.Sort(told)
	if len(read) == 0 || !slices.Equal(told, read) {
		t.Errorf("the worker is told %v, hq_runner reads %v", told, read)
	}
}

// A token root replaces while the worker runs reaches the worker, already
// its own when it takes the name.
func TestTheWorkersTokenIsKeptCurrent(t *testing.T) {
	w := newWorker(t)
	run := t.TempDir()
	if err := os.Mkdir(filepath.Join(run, "token"), 0o700); err != nil {
		t.Fatal(err)
	}
	lent, err := w.a.lendToken(run, w.grant.Path, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(w.grant.Path); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(w.grant.Path, []byte("example-token-two"), 0o400); err != nil {
		t.Fatal(err)
	}
	w.owned = nil

	if _, err := w.a.lendToken(run, w.grant.Path, lent); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(filepath.Join(run, "token", automationTokenName))
	if err != nil || string(data) != "example-token-two" {
		t.Fatalf("the worker reads %q: %v", data, err)
	}
	if len(w.owned) != 1 || w.owned[0] == automationTokenName {
		t.Errorf("the token is given to the worker as %v, not before it takes its name", w.owned)
	}
	if entries, _ := os.ReadDir(filepath.Join(run, "token")); len(entries) != 1 {
		t.Errorf("the token directory holds %d files", len(entries))
	}
}

// A worker that outlives its bound is stopped and its container removed.
func TestAWorkerPastItsBoundIsRemoved(t *testing.T) {
	w := newWorker(t)
	w.a.Bound = 50 * time.Millisecond
	w.docker = "exec sleep 60"

	err := w.a.Run(context.Background())

	if err == nil {
		t.Fatal("a worker past its bound ended well")
	}
	last := w.calls[len(w.calls)-1]
	if !slices.Equal(last[:4], []string{"docker", "rm", "--force", w.calls[len(w.calls)-2][slices.Index(w.calls[len(w.calls)-2], "--name")+1]}) {
		t.Errorf("the container was left: %q", w.calls)
	}
}

// Nothing runs where there is nothing to run, or nothing to run it safely
// with: no automation entry, a release with no worker image, a grant whose
// token is not placed yet, a table that cannot be loaded.
func TestNoWorkerStartsWithoutWhatItNeeds(t *testing.T) {
	for name, change := range map[string]func(w *worker) (fails bool){
		"no automation entry": func(w *worker) bool { w.a.Project = nil; return false },
		"no image":            func(w *worker) bool { _ = os.Remove(filepath.Join(w.a.Lib, AutomationImage)); return false },
		"no token yet":        func(w *worker) bool { _ = os.Remove(w.grant.Path); return true },
		"no journal":          func(w *worker) bool { _ = os.Remove(w.a.Journal); return true },
		"an unpinned image": func(w *worker) bool {
			_ = os.Remove(filepath.Join(w.a.Lib, AutomationImage))
			_ = os.WriteFile(filepath.Join(w.a.Lib, AutomationImage), []byte("ghcr.io/example/automation:latest\n"), 0o400)
			return true
		},
	} {
		t.Run(name, func(t *testing.T) {
			w := newWorker(t)
			fails := change(w)

			err := w.a.Run(context.Background())

			if fails != (err != nil) {
				t.Errorf("Run returned %v", err)
			}
			if slices.ContainsFunc(w.calls, runsContainer) {
				t.Errorf("a worker ran: %q", w.calls)
			}
		})
	}
}

// A host's automation entry is refused unless its repository is GitHub's to
// name, its directory stays inside it, and its grant reads it.
func TestAHostsAutomationEntryIsReadStrictly(t *testing.T) {
	grant := `{"name": "automation-projects", "repositories": ["example/playbooks"], "permissions": {"contents": "read"}, "path": "/run/example-automation/github_token"}`
	declared := func(automation string) string {
		return strings.Replace(hostJSON, `"vault"`, `"grants": [`+grant+`], "automation": `+automation+`, "vault"`, 1)
	}
	valid := `{"repository": "example/playbooks", "directory": "ansible", "grant": "automation-projects"}`
	config, err := ReadConfig([]byte(declared(valid)))
	if err != nil || config.Automation == nil || config.Automation.Grant != "automation-projects" {
		t.Fatalf("a valid entry is refused: %v", err)
	}
	if _, err := ReadConfig([]byte(declared(`{"repository": "example/public"}`))); err != nil {
		t.Errorf("a public repository's entry is refused: %v", err)
	}
	for name, automation := range map[string]string{
		"no repository":            `{"directory": "ansible"}`,
		"a repository that is not": `{"repository": "example"}`,
		"a directory outside":      `{"repository": "example/playbooks", "directory": "../ansible"}`,
		"an absolute directory":    `{"repository": "example/playbooks", "directory": "/ansible"}`,
		"a grant the host lacks":   `{"repository": "example/playbooks", "grant": "example-other"}`,
		"a grant of another repo":  `{"repository": "example/other", "grant": "automation-projects"}`,
		"a key it does not know":   `{"repository": "example/playbooks", "revision": "main"}`,
	} {
		if _, err := ReadConfig([]byte(declared(automation))); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestTheExampleHostRunsAWorker(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "deploy", "host.example.json"))
	if err != nil {
		t.Fatal(err)
	}
	config, err := ReadConfig(data)
	if err != nil || config.Automation == nil || config.Automation.Grant == "" {
		t.Fatalf("the example declares no worker with a grant: %v", err)
	}
}

// The units start the worker the way the launcher and HQ expect: its path
// waits on the doorbell HQ rings, the service removes it before claiming,
// runs hq-host automation and outlasts the bound, and the tmpfiles rule
// makes its journal its own.
func TestTheAutomationUnitsAreWhereTheLayoutSays(t *testing.T) {
	systemd := filepath.Join("..", "..", "deploy", "systemd")
	path := filepath.Join(systemd, "severino-hq-automation.path")
	if got := unitValue(t, path, "PathExists"); got != AutomationDoorbell {
		t.Errorf("the path waits on %q, not %s", got, AutomationDoorbell)
	}
	if got := unitValue(t, path, "Unit"); got != AutomationUnit {
		t.Errorf("the path starts %q", got)
	}
	if got := unitValue(t, filepath.Join(systemd, "severino-hq-automation.timer"), "Unit"); got != AutomationUnit {
		t.Errorf("the timer starts %q", got)
	}
	service := filepath.Join(systemd, AutomationUnit)
	if got := unitValue(t, service, "ExecStartPre"); got != "/bin/rm -f "+AutomationDoorbell {
		t.Errorf("the worker does not remove its doorbell before it claims: %q", got)
	}
	if got := unitValue(t, service, "ExecStart"); got != HostProgram+" automation" {
		t.Errorf("the unit runs %q", got)
	}
	if got, bound := unitSetting(t, AutomationUnit, "TimeoutStartSec"), AutomationBound+time.Minute; got <= bound {
		t.Errorf("the unit stops the worker at %s, before its removal at %s", got, bound)
	}
	writable := strings.Fields(unitValue(t, service, "ReadWritePaths"))
	for _, dir := range []string{hostcore.SecretDir, hostcore.RunDir} {
		if !slices.Contains(writable, dir) {
			t.Errorf("the worker's unit may not write %s", dir)
		}
	}
	rules, err := os.ReadFile(filepath.Join("..", "..", "deploy", "tmpfiles", "severino-hq.conf"))
	if err != nil {
		t.Fatal(err)
	}
	if want := fmt.Sprintf("d %s 0700 %d %d -", AutomationState, hostcore.WebUID, hostcore.WebGID); !slices.Contains(strings.Split(string(rules), "\n"), want) {
		t.Errorf("no tmpfiles rule %q makes the journal the worker's alone", want)
	}
}

// The dry launch: what root runs on a host for the example configuration,
// printed with -v.
func TestADryLaunchShowsTheWorkersContainer(t *testing.T) {
	w := newWorker(t)
	w.a.SecretDir = filepath.Join(w.root, "secrets")

	args := w.run(t)

	run := ""
	for _, arg := range args {
		if source, target, ok := bind(arg); ok && target == automationConnections {
			run = filepath.Dir(source)
		}
	}
	var lines []string
	for i := 0; i < len(args); i++ {
		if strings.HasPrefix(args[i], "--") && !slices.Contains([]string{"--rm", "--read-only", "--no-healthcheck"}, args[i]) {
			lines = append(lines, args[i]+" "+args[i+1])
			i++
			continue
		}
		lines = append(lines, args[i])
	}
	shown := strings.NewReplacer(run, hostcore.SecretDir+"/automation.<run>", w.a.Journal, AutomationState).Replace("docker " + strings.Join(lines, " \\\n  "))
	t.Logf("\n%s", shown)
	loaded, _ := os.ReadFile(filepath.Join(w.root, "loaded.nft"))
	t.Logf("nft -f -\n%s", loaded)
}
