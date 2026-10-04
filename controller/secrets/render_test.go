package secrets

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/secrets/connect"
	"github.com/joeseverino/severino-hq/controller/secrets/connecttest"
	"github.com/joeseverino/severino-hq/controller/secrets/install"
	"github.com/joeseverino/severino-hq/controller/secrets/project"
)

type (
	item  = connecttest.Item
	field = connecttest.Field
)

var f, b, id = connecttest.F, connecttest.B, connecttest.ID

// Values that must never reach a log line, an error or the status document.
const (
	secretToken  = "sentinel-provider-token-value"
	secretEnv    = "sentinel-application-secret"
	secretWriter = "sentinel-writer-token-value"
)

// The host key connections pin: a fresh one each run, never a literal.
var hostKey = connecttest.HostKey()

type fakeWeb struct {
	installed bool
	restarts  int
	health    []string
	asked     int
}

func (w *fakeWeb) Installed(context.Context) bool { return w.installed }
func (w *fakeWeb) Restart(context.Context) error  { w.restarts++; return nil }
func (w *fakeWeb) Health(context.Context) string {
	w.asked++
	if len(w.health) == 0 {
		return "healthy"
	}
	next := w.health[0]
	if len(w.health) > 1 {
		w.health = w.health[1:]
	}
	return next
}

// host is a machine: a Connect, the directories, the unit's credentials.
type host struct {
	t        *testing.T
	fake     *connecttest.Fake
	runner   *Runner
	web      *fakeWeb
	log      *bytes.Buffer
	now      time.Time
	mountErr error
	creds    string
}

func envItem() item {
	fields := []field{f("DJANGO_SECRET_KEY", secretEnv)}
	for index := range 15 {
		fields = append(fields, f("EXAMPLE_"+string(rune('A'+index)), "value"))
	}
	return item{ID: id(900), Title: "example env", Version: 1, Fields: fields}
}

func apiToken(n int, ref, prefix string) item {
	return item{ID: id(n), Title: "Example " + ref, Version: 1, Fields: []field{
		f("connection_ref", ref), f("projection", "api_token"), f("env_prefix", prefix),
		b("credential", secretToken), f("website", "https://api.example.com"),
	}}
}

func sshConnection(n int, ref, prefix, identity string) item {
	return item{ID: id(n), Version: 1, Fields: []field{
		f("connection_ref", ref), f("projection", "ssh_transport"), f("env_prefix", prefix),
		f("host", "edge.example.com"), f("port", "2222"), f("user", "deploy"),
		f("host_key", hostKey), f("identity", identity),
	}}
}

func newHost(t *testing.T, items ...item) *host {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	h := &host{t: t, web: &fakeWeb{}, log: &bytes.Buffer{}, now: time.Date(2026, 10, 4, 12, 0, 0, 0, time.UTC),
		creds: filepath.Join(root, "credentials")}
	h.fake = connecttest.New(t, append(items, envItem())...)
	runtime := filepath.Join(root, "runtime")
	for _, dir := range []string{runtime, h.creds} {
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	h.credential("op_connect_example", connecttest.Token+"\n")
	h.runner = &Runner{
		Config: Config{
			Vault: connecttest.VaultName, EnvItem: "example env", Endpoint: h.fake.Server.URL,
			CredentialsDir: h.creds, ConnectCredential: "op_connect_example",
			Layout: install.Layout{SecretDir: filepath.Join(root, "secrets"), RuntimeDir: runtime, WebDir: filepath.Join(runtime, "web"),
				RootUID: os.Getuid(), RootGID: os.Getgid(), WebUID: os.Getuid(), WebGID: os.Getgid()},
			RegistryPath: "../../hq/config/controller-connections.json", MinAppVariables: 15,
			ConnectTimeout: 5 * time.Second, FullEvery: 24 * time.Hour, HealthAttempts: 12, HealthInterval: 5 * time.Second,
		},
		Mounts: func(string) error { return h.mountErr },
		Web:    h.web,
		Log:    slog.New(slog.NewTextHandler(h.log, &slog.HandlerOptions{Level: slog.LevelDebug})),
		Now:    func() time.Time { return h.now },
		Sleep:  func(context.Context, time.Duration) error { return nil },
	}
	return h
}

func (h *host) credential(name, value string) {
	h.t.Helper()
	if err := os.WriteFile(filepath.Join(h.creds, name), []byte(value), 0o400); err != nil {
		os.Chmod(filepath.Join(h.creds, name), 0o600)
		if err := os.WriteFile(filepath.Join(h.creds, name), []byte(value), 0o400); err != nil {
			h.t.Fatal(err)
		}
	}
}

func (h *host) layout() install.Layout { return h.runner.Config.Layout }
func (h *host) appEnv() string         { return filepath.Join(h.layout().WebDir, install.AppEnvName) }
func (h *host) document() string {
	return filepath.Join(h.layout().RuntimeDir, install.ConnectionsName)
}
func (h *host) ssh(name string) string {
	return filepath.Join(h.layout().RuntimeDir, install.SSHDirName, name)
}

func (h *host) run() (Result, error) {
	h.t.Helper()
	result, err := h.runner.Run(context.Background())
	h.noLeak(err)
	return result, err
}

func (h *host) ok() Result {
	h.t.Helper()
	result, err := h.run()
	if err != nil {
		h.t.Fatalf("render failed: %v\n%s", err, h.log)
	}
	return result
}

// noLeak asserts that no credential reached the log, the error, the status
// document, or a file the renderer leaves readable beyond its own.
func (h *host) noLeak(err error) {
	h.t.Helper()
	surfaces := map[string]string{"log": h.log.String()}
	if err != nil {
		surfaces["error"] = err.Error()
	}
	if status, readErr := os.ReadFile(filepath.Join(h.layout().RuntimeDir, StatusName)); readErr == nil {
		surfaces["status document"] = string(status)
	}
	for where, text := range surfaces {
		for _, secret := range []string{connecttest.Token, secretToken, secretEnv, secretWriter, "PRIVATE KEY"} {
			if strings.Contains(text, secret) {
				h.t.Fatalf("the %s carries a credential (%s...)", where, secret[:12])
			}
		}
		if strings.Contains(text, connecttest.VaultName) || strings.Contains(text, "Example ") || strings.Contains(text, "example env") {
			h.t.Fatalf("the %s names the vault or an item: %s", where, text)
		}
	}
}

func (h *host) status() Status {
	h.t.Helper()
	data, err := os.ReadFile(filepath.Join(h.layout().RuntimeDir, StatusName))
	if err != nil {
		h.t.Fatal(err)
	}
	status, err := DecodeStatus(data)
	if err != nil {
		h.t.Fatalf("the status document is not its declared shape: %v\n%s", err, data)
	}
	return status
}

func (h *host) staging() []string {
	found, _ := filepath.Glob(filepath.Join(h.layout().RuntimeDir, ".refresh.*"))
	return found
}

func inode(t *testing.T, path string) uint64 {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return uint64(info.Sys().(*syscall.Stat_t).Ino)
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func TestAFirstRenderInstallsEveryConsumersFile(t *testing.T) {
	key := connecttest.Ed25519Key(t)
	h := newHost(t, apiToken(1, "example", "EXAMPLE"), sshConnection(2, "edge", "EDGE", "Edge deploy key"),
		connecttest.KeyItem(id(3), "Edge deploy key", key.PKCS8, key.Public))
	h.web.installed = true
	// A token file an earlier release kept: nothing accepts one.
	os.MkdirAll(h.layout().SecretDir, 0o700)
	token := filepath.Join(h.layout().SecretDir, "severino_mcp_token")
	os.WriteFile(token, []byte("previous-value"), 0o400)

	result := h.ok()
	if result.Outcome != "rendered" || !result.Changed || !result.WebChanged || !result.Restarted || h.web.restarts != 1 {
		t.Fatalf("result: %+v", result)
	}
	env := read(t, h.appEnv())
	if !strings.HasPrefix(env, "DJANGO_SECRET_KEY='"+secretEnv+"'\nEXAMPLE_A='value'\n") || strings.Count(env, "\n") != 16 || mode(t, h.appEnv()) != 0o400 {
		t.Fatalf("application environment (mode %o):\n%s", mode(t, h.appEnv()), env)
	}
	if mode(t, h.document()) != 0o400 {
		t.Fatalf("connections document mode %o", mode(t, h.document()))
	}
	document, err := connections.ReadFile(h.document(), os.Getuid())
	if err != nil {
		t.Fatalf("the controller would refuse the document: %v", err)
	}
	entries := document.Entries()
	if entries["EXAMPLE_API_TOKEN"] != secretToken || entries["EDGE_HOST"] != "edge.example.com" ||
		entries["EXAMPLE_STORE_VAULT"] != connecttest.VaultName || entries["EXAMPLE_STORE_ITEM"] != id(1) {
		t.Fatal("the document does not hold the connections")
	}
	if read(t, h.ssh("known_hosts")) != "[edge.example.com]:2222 "+hostKey+"\n" || mode(t, h.ssh("edge")) != 0o400 ||
		read(t, h.ssh("edge.pub")) != key.Public+"\n" || mode(t, h.ssh("edge.pub")) != 0o444 {
		t.Fatal("identities")
	}
	if _, err := os.Lstat(token); err == nil {
		t.Fatal("the retired MCP token file was kept")
	}
	if len(h.staging()) != 0 {
		t.Fatalf("staging was left behind: %v", h.staging())
	}
	for _, dir := range []string{h.layout().RuntimeDir, h.layout().WebDir, h.layout().SecretDir, filepath.Dir(h.ssh("x"))} {
		if mode(t, dir) != 0o700 {
			t.Fatalf("%s is mode %o", dir, mode(t, dir))
		}
	}
	status := h.status()
	if status.LastAttempt.Outcome != "rendered" || status.LastAttempt.Failure != "" || status.LastSuccess == nil ||
		!status.LastSuccess.At.Equal(h.now) || *status.LastSuccess.ContentVersion != 1 ||
		status.LastSuccess.Counts != (Counts{ItemsRead: 4, Connections: 2, AppVariables: 16, Identities: 1}) {
		t.Fatalf("status: %+v %+v", status.LastAttempt, status.LastSuccess)
	}
	if status.Connect == nil || status.Connect.Version != "1.8.1" || status.Connect.Dependencies[0] != (Dependency{Service: "sync", Status: "ACTIVE"}) {
		t.Fatalf("connect status: %+v", status.Connect)
	}
	// The token went to Connect and nowhere else: not to a file, not to the
	// unauthenticated health read.
	filepath.WalkDir(filepath.Dir(h.layout().RuntimeDir), func(path string, entry os.DirEntry, _ error) error {
		if entry.Type().IsRegular() && !strings.HasPrefix(path, h.creds) && strings.Contains(read(t, path), connecttest.Token) {
			t.Fatalf("the reader token was written to %s", path)
		}
		return nil
	})
}

func TestAnUnchangedVaultIsNotReReadItemByItem(t *testing.T) {
	key := connecttest.Ed25519Key(t)
	h := newHost(t, apiToken(1, "example", "EXAMPLE"), sshConnection(2, "edge", "EDGE", "Edge deploy key"),
		connecttest.KeyItem(id(3), "Edge deploy key", key.PKCS8, key.Public))
	h.web.installed = true
	h.ok()
	items := "/v1/vaults/" + connecttest.VaultID + "/items"
	readItems := h.fake.Count(items)
	inodes := []uint64{inode(t, h.appEnv()), inode(t, h.document()), inode(t, h.ssh("edge"))}

	unchanged := func(why string) {
		t.Helper()
		before := len(h.fake.Requests)
		result := h.ok()
		if result.Outcome != "current" || result.Changed || h.fake.Count(items) != readItems {
			t.Fatalf("%s: an unchanged vault was read again: %+v", why, result)
		}
		if asked := len(h.fake.Requests) - before; asked != 2 {
			t.Fatalf("%s: an unchanged hour cost %d requests, wanted the vault listing and health", why, asked)
		}
		if inodes[0] != inode(t, h.appEnv()) || inodes[1] != inode(t, h.document()) || inodes[2] != inode(t, h.ssh("edge")) {
			t.Fatalf("%s: an unchanged run replaced a file", why)
		}
	}
	rendered := func(why string) Result {
		t.Helper()
		result := h.ok()
		if result.Outcome != "rendered" || h.fake.Count(items) == readItems {
			t.Fatalf("%s: the vault was not read again: %+v", why, result)
		}
		readItems = h.fake.Count(items)
		inodes = []uint64{inode(t, h.appEnv()), inode(t, h.document()), inode(t, h.ssh("edge"))}
		return result
	}

	h.now = h.now.Add(time.Hour)
	unchanged("an hour later")
	if status := h.status(); status.LastAttempt.Outcome != "current" || !status.LastSuccess.At.Equal(h.now) ||
		!status.LastSuccess.RenderedAt.Equal(h.now.Add(-time.Hour)) || status.LastSuccess.Counts.Connections != 2 {
		t.Fatalf("status after an unchanged run: %+v %+v", status.LastAttempt, status.LastSuccess)
	}
	if h.web.restarts != 1 {
		t.Fatal("an unchanged run restarted the web container")
	}

	// A full read of unchanged content installs nothing: same inodes, even
	// for the identity, whose OpenSSH encoding differs on every render.
	h.fake.Edit(func(*connecttest.Fake) {})
	previous := inodes
	if result := rendered("a new content version"); result.Changed || previous[0] != inodes[0] || previous[1] != inodes[1] || previous[2] != inodes[2] {
		t.Fatalf("re-rendering unchanged content replaced a file: %+v", result)
	}
	unchanged("after the re-read")

	// A rotated credential reaches the controller, and only the controller.
	h.fake.Edit(func(fake *connecttest.Fake) {
		rotated := secretToken + "-rotated"
		fake.Items[0].Fields[3].Value = &rotated
		fake.Items[0].Version++
	})
	previous = inodes
	if result := rendered("a rotated credential"); !result.Changed || result.WebChanged || previous[1] == inodes[1] || previous[0] != inodes[0] {
		t.Fatalf("rotation: %+v", result)
	}
	if h.web.restarts != 1 {
		t.Fatal("a controller-only change restarted the web container")
	}

	// Installed files are verified, not assumed.
	for why, tamper := range map[string]func(){
		"an edited document": func() {
			os.Chmod(h.document(), 0o600)
			os.WriteFile(h.document(), []byte("{}"), 0o400)
			os.Chmod(h.document(), 0o400)
		},
		"a removed identity":        func() { os.Remove(h.ssh("edge")) },
		"a removed app environment": func() { os.Remove(h.appEnv()) },
		"an added identity":         func() { os.WriteFile(h.ssh("planted"), []byte("x"), 0o400) },
		"a loosened mode":           func() { os.Chmod(h.document(), 0o444) },
		"a removed state file":      func() { os.Remove(filepath.Join(h.layout().RuntimeDir, stateName)) },
		"a linked document": func() {
			os.Rename(h.document(), h.document()+".moved")
			os.Symlink(h.document()+".moved", h.document())
		},
	} {
		unchanged("before " + why)
		tamper()
		rendered(why)
		os.Remove(h.document() + ".moved")
		if _, err := connections.ReadFile(h.document(), os.Getuid()); err != nil {
			t.Fatalf("%s was not repaired: %v", why, err)
		}
		if _, err := os.Lstat(h.ssh("planted")); err == nil {
			t.Fatalf("%s: a planted identity was kept", why)
		}
	}

	// A full read at least once a day, whatever the versions say.
	unchanged("before a day passes")
	h.now = h.now.Add(25 * time.Hour)
	rendered("a day without a full read")
	// A clock set back does not make an old render look fresh forever.
	h.now = h.now.Add(-48 * time.Hour)
	rendered("a clock set back")
}

func TestTheRegistryAndConfigurationArePartOfWhatIsCurrent(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	shipped := read(t, h.runner.Config.RegistryPath)
	registry := filepath.Join(filepath.Dir(h.creds), "registry.json")
	os.WriteFile(registry, []byte(shipped), 0o600)
	h.runner.Config.RegistryPath = registry
	h.ok()
	if h.ok().Outcome != "current" {
		t.Fatal("the second run was not current")
	}
	os.WriteFile(registry, []byte(strings.ReplaceAll(shipped, `"label": "website"`, `"label": "address"`)), 0o600)
	if _, err := h.run(); !errors.Is(err, project.ErrContent) {
		t.Fatalf("a changed registry was not applied: %v", err)
	}
	os.WriteFile(registry, []byte(`{"schema_version":1,"projections":{},"connections":{}}`), 0o600)
	if _, err := h.run(); Class(err) != "config" {
		t.Fatalf("a registry naming connections was accepted: %v", err)
	}
	os.Remove(registry)
	if _, err := h.run(); Class(err) != "config" {
		t.Fatalf("a missing registry was accepted: %v", err)
	}
}

func TestARefreshKeepsTheAppEnvironmentInodeAndReplacesTheDocuments(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	layout := h.layout()
	for _, dir := range []string{layout.SecretDir, layout.WebDir} {
		os.MkdirAll(dir, 0o700)
	}
	checkout := filepath.Join(layout.SecretDir, install.AppEnvName)
	for _, path := range []string{h.appEnv(), checkout, h.document()} {
		os.WriteFile(path, []byte("previous-value"), 0o400)
	}
	h.web.installed = true
	before := []uint64{inode(t, h.appEnv()), inode(t, checkout), inode(t, h.document())}
	result := h.ok()
	if !result.WebChanged || h.web.restarts != 1 {
		t.Fatalf("result: %+v", result)
	}
	// Bind-mounted into a running container: the same inode, new content.
	if inode(t, h.appEnv()) != before[0] || inode(t, checkout) != before[1] {
		t.Fatal("the application environment lost its inode: the container would keep the old file")
	}
	if read(t, h.appEnv()) == "previous-value" || read(t, checkout) != read(t, h.appEnv()) || mode(t, checkout) != 0o400 {
		t.Fatal("the application environment was not rewritten in both places")
	}
	// No persistent bind mount: a rename gives each reader a whole old or new file.
	if inode(t, h.document()) == before[2] || mode(t, h.document()) != 0o400 {
		t.Fatal("the connections document was rewritten in place")
	}
	current := []uint64{inode(t, h.appEnv()), inode(t, checkout), inode(t, h.document())}
	if repeated := h.ok(); repeated.Outcome != "current" || repeated.Changed {
		t.Fatalf("a repeated refresh was not current: %+v", repeated)
	}
	if !strings.Contains(h.log.String(), "secrets are current") {
		t.Fatal("a current run did not say so")
	}
	if current[0] != inode(t, h.appEnv()) || current[1] != inode(t, checkout) || current[2] != inode(t, h.document()) {
		t.Fatal("a repeated refresh replaced a file")
	}
	// The deploy that binds the tmpfs copy removes the checkout's: the next
	// render does not put it back.
	os.Remove(checkout)
	h.fake.Edit(func(*connecttest.Fake) {})
	h.ok()
	if _, err := os.Lstat(checkout); err == nil {
		t.Fatal("the checkout's copy was recreated")
	}
}

func TestAFailedRefreshPreservesEveryInstalledFile(t *testing.T) {
	key, other := connecttest.Ed25519Key(t), connecttest.Ed25519Key(t)
	edge := sshConnection(2, "edge", "EDGE", "Edge deploy key")
	keyItem := connecttest.KeyItem(id(3), "Edge deploy key", key.PKCS8, key.Public)
	items := "/v1/vaults/" + connecttest.VaultID + "/items"
	answer := func(match func(*http.Request) bool, status int, body string) func(http.ResponseWriter, *http.Request) bool {
		return func(w http.ResponseWriter, r *http.Request) bool {
			if !match(r) {
				return false
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			w.Write([]byte(body))
			return true
		}
	}
	listing := func(r *http.Request) bool { return r.URL.Path == items }
	oneItem := func(r *http.Request) bool { return r.URL.Path == items+"/"+id(1) }
	cases := []struct {
		name   string
		class  string
		change func(*host)
	}{
		{"Connect unavailable", "connect_unavailable", func(h *host) {
			h.runner.Config.ConnectTimeout = 100 * time.Millisecond
			h.fake.Unavailable = 1 << 30
		}},
		{"Connect not listening", "connect_unavailable", func(h *host) {
			h.runner.Config.ConnectTimeout = 100 * time.Millisecond
			h.fake.Server.Close()
		}},
		{"token refused", "connect_denied", func(h *host) { h.credential("op_connect_example", "another.token\n") }},
		{"empty token", "config", func(h *host) { h.credential("op_connect_example", "\n") }},
		{"malformed token", "config", func(h *host) { h.credential("op_connect_example", "two words\n") }},
		{"missing credential", "config", func(h *host) { os.Remove(filepath.Join(h.creds, "op_connect_example")) }},
		{"no credentials directory", "config", func(h *host) { h.runner.Config.CredentialsDir = "" }},
		{"credential name leaves the directory", "config", func(h *host) { h.runner.Config.ConnectCredential = "../op_connect_example" }},
		{"remote endpoint", "config", func(h *host) { h.runner.Config.Endpoint = "https://example.com" }},
		{"listing fails", "connect_unavailable", func(h *host) { h.fake.Intercept = answer(listing, 500, `{}`) }},
		{"listing denied", "connect_denied", func(h *host) { h.fake.Intercept = answer(listing, 403, `{}`) }},
		{"listing malformed", "connect_response", func(h *host) { h.fake.Intercept = answer(listing, 200, `{"not":"a list"}`) }},
		{"listing empty", "content", func(h *host) { h.fake.Intercept = answer(listing, 200, `[]`) }},
		{"an item fails", "connect_unavailable", func(h *host) { h.fake.Intercept = answer(oneItem, 502, `{}`) }},
		{"an item is missing", "connect_response", func(h *host) { h.fake.Intercept = answer(oneItem, 404, `{}`) }},
		{"an item is malformed", "connect_response", func(h *host) { h.fake.Intercept = answer(oneItem, 200, `{"id":7}`) }},
		{"the vault is not in scope", "connect_response", func(h *host) { h.runner.Config.Vault = "Another Vault" }},
		{"no connections", "content", func(h *host) { h.fake.Items = []item{envItem()} }},
		{"no environment item", "content", func(h *host) { h.fake.Items = h.fake.Items[:3] }},
		{"a small environment", "content", func(h *host) {
			env := envItem()
			env.Fields = env.Fields[:14]
			h.fake.Items[3] = env
		}},
		{"duplicate connection", "content", func(h *host) { h.fake.Items = append(h.fake.Items, apiToken(5, "example", "OTHER")) }},
		{"identity halves differ", "content", func(h *host) {
			h.fake.Items[2] = connecttest.KeyItem(id(3), "Edge deploy key", key.PKCS8, other.Public)
		}},
		{"a multi-line credential", "content", func(h *host) {
			value := secretToken + "\nINJECTED=x"
			h.fake.Items[0].Fields[3].Value = &value
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHost(t, apiToken(1, "example", "EXAMPLE"), edge, keyItem)
			h.web.installed = true
			h.ok()
			h.log.Reset()
			paths := []string{h.appEnv(), h.document(), h.ssh("edge"), h.ssh("edge.pub"), h.ssh("known_hosts")}
			type snapshot struct {
				content string
				inode   uint64
			}
			before := map[string]snapshot{}
			for _, path := range paths {
				before[path] = snapshot{read(t, path), inode(t, path)}
			}
			succeeded := h.status().LastSuccess
			h.fake.Lock()
			h.fake.ContentVersion++
			c.change(h)
			h.fake.Unlock()
			h.now = h.now.Add(time.Hour)

			result, err := h.run()
			if err == nil || Class(err) != c.class {
				t.Fatalf("wanted a %s failure, got %q: %v", c.class, Class(err), err)
			}
			if result.Changed || h.web.restarts != 1 {
				t.Fatalf("a failed run changed something: %+v", result)
			}
			for _, path := range paths {
				if read(t, path) != before[path].content || inode(t, path) != before[path].inode {
					t.Fatalf("%s was touched by a failed refresh", filepath.Base(path))
				}
			}
			if len(h.staging()) != 0 {
				t.Fatalf("staging was left behind: %v", h.staging())
			}
			// The failure is on record, and the last success is still the last success.
			status := h.status()
			if c.name == "remote endpoint" {
				// Refused before the host is opened: nothing is written at all.
				if !status.LastAttempt.At.Equal(succeeded.At) {
					t.Fatalf("a refused configuration wrote the status document: %+v", status.LastAttempt)
				}
				return
			}
			if status.LastAttempt.Outcome != "failed" || status.LastAttempt.Failure != c.class || !status.LastAttempt.At.Equal(h.now) {
				t.Fatalf("status attempt: %+v", status.LastAttempt)
			}
			if status.LastSuccess == nil || !status.LastSuccess.At.Equal(succeeded.At) || status.LastSuccess.Counts != succeeded.Counts {
				t.Fatalf("a failure rewrote the last success: %+v", status.LastSuccess)
			}
		})
	}
}

func TestAnUnsafeHostIsRefusedBeforeAnythingIsRead(t *testing.T) {
	for name, change := range map[string]func(*host){
		"disk-backed runtime": func(h *host) {
			h.mountErr = errors.Join(install.ErrHost, errors.New("Controller secret directory must be on tmpfs."))
		},
		"swappable runtime": func(h *host) {
			h.runner.Mounts = func(dir string) error {
				return install.CheckMountInfo([]byte("90 25 0:50 / "+dir+" rw - tmpfs tmpfs rw,nosuid,noswapfile\n"), dir)
			}
		},
		"runtime open to others":   func(h *host) { os.Chmod(h.layout().RuntimeDir, 0o755) },
		"shared with the doorbell": func(h *host) { h.runner.Config.Layout.RuntimeDir = "/run/severino-hq" },
		"private keys left on disk": func(h *host) {
			legacy := filepath.Join(h.layout().SecretDir, "ssh")
			os.MkdirAll(legacy, 0o700)
			os.WriteFile(filepath.Join(legacy, "edge"), []byte("-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n"), 0o600)
		},
	} {
		t.Run(name, func(t *testing.T) {
			h := newHost(t, apiToken(1, "example", "EXAMPLE"))
			// The credential cannot be read: a run that reached for it fails as
			// configuration, not as the host.
			os.Remove(filepath.Join(h.creds, "op_connect_example"))
			change(h)
			_, err := h.runner.Run(context.Background())
			if Class(err) != "host" {
				t.Fatalf("wanted a host refusal, got %q: %v", Class(err), err)
			}
			if len(h.fake.Requests) != 0 {
				t.Fatalf("Connect was asked %v before the host was accepted", h.fake.Requests)
			}
			if _, statErr := os.Lstat(h.appEnv()); statErr == nil {
				t.Fatal("a refused host was written to")
			}
		})
	}
	// Public halves alone on disk do not stop the refresh.
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	os.MkdirAll(filepath.Join(h.layout().SecretDir, "ssh"), 0o700)
	os.WriteFile(filepath.Join(h.layout().SecretDir, "ssh", "edge.pub"), []byte("ssh-ed25519 AAAA example\n"), 0o644)
	h.ok()
}

func TestDestinationsThatAreNotTheRenderersOwnAreRefused(t *testing.T) {
	for name, plant := range map[string]func(h *host, system string){
		"a symlinked app environment": func(h *host, system string) { os.Symlink(system, h.appEnv()) },
		"a hard-linked app environment": func(h *host, system string) {
			if err := os.Link(system, h.appEnv()); err != nil {
				h.t.Fatal(err)
			}
		},
		"a directory at the app environment": func(h *host, _ string) { os.Mkdir(h.appEnv(), 0o700) },
		"a symlinked checkout copy": func(h *host, system string) {
			os.Symlink(system, filepath.Join(h.layout().SecretDir, install.AppEnvName))
		},
	} {
		t.Run(name, func(t *testing.T) {
			h := newHost(t, apiToken(1, "example", "EXAMPLE"))
			system := filepath.Join(filepath.Dir(h.creds), "system-file")
			os.WriteFile(system, []byte("system"), 0o600)
			for _, dir := range []string{h.layout().SecretDir, h.layout().WebDir} {
				os.MkdirAll(dir, 0o700)
			}
			plant(h, system)
			if _, err := h.run(); Class(err) != "host" {
				t.Fatalf("wanted a host refusal, got %q: %v", Class(err), err)
			}
			if read(t, system) != "system" {
				t.Fatal("the planted destination was written through")
			}
		})
	}
	// The controller's own files are replaced, never written through.
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	system := filepath.Join(filepath.Dir(h.creds), "system-file")
	os.WriteFile(system, []byte("system"), 0o600)
	os.Symlink(system, h.document())
	h.ok()
	if read(t, system) != "system" || mode(t, h.document()) != 0o400 {
		t.Fatal("a link at the connections document was written through")
	}
}

func TestIdentitiesFollowTheirConnections(t *testing.T) {
	key, other := connecttest.Ed25519Key(t), connecttest.Ed25519Key(t)
	signing := connecttest.RSAKey(t)
	h := newHost(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
		connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, key.Public),
		item{ID: id(3), Version: 1, Fields: []field{f("connection_ref", "github"), f("projection", "github_app"),
			f("env_prefix", "GITHUB"), f("app_id", "12345"), f("signing_key", "Example app key")}},
		connecttest.KeyItem(id(4), "Example app key", signing.PKCS8, signing.Public))
	// What an earlier render left for a connection that no longer exists.
	os.MkdirAll(filepath.Dir(h.ssh("x")), 0o700)
	os.WriteFile(h.ssh("retired"), []byte("an old key"), 0o400)
	os.WriteFile(h.ssh("retired.pub"), []byte("an old key"), 0o444)
	h.ok()
	for _, name := range []string{"retired", "retired.pub"} {
		if _, err := os.Lstat(h.ssh(name)); err == nil {
			t.Fatalf("%s outlived its connection", name)
		}
	}
	if read(t, h.ssh("github.key")) != signing.PKCS8 || mode(t, h.ssh("github.key")) != 0o400 || read(t, h.ssh("github.key.pub")) != signing.Public+"\n" {
		t.Fatal("the signing key was not rendered as the item's PKCS#8 key")
	}
	if status := h.status(); status.LastSuccess.Counts.Identities != 1 || status.LastSuccess.Counts.SigningKeys != 1 {
		t.Fatalf("counts: %+v", status.LastSuccess.Counts)
	}
	// A rotated identity replaces the old one, and its pinned host stays.
	previous := read(t, h.ssh("edge"))
	h.fake.Edit(func(fake *connecttest.Fake) {
		fake.Items[1] = connecttest.KeyItem(id(2), "Edge deploy key", other.PKCS8, other.Public)
	})
	if result := h.ok(); !result.Changed || result.WebChanged {
		t.Fatalf("rotation: %+v", result)
	}
	if read(t, h.ssh("edge")) == previous || read(t, h.ssh("edge.pub")) != other.Public+"\n" {
		t.Fatal("the rotated identity was not installed")
	}
	// A connection removed from the vault takes its identity and signing key.
	h.fake.Edit(func(fake *connecttest.Fake) { fake.Items = fake.Items[2:] })
	h.ok()
	entries, _ := os.ReadDir(filepath.Dir(h.ssh("x")))
	if len(entries) != 3 {
		t.Fatalf("after removal the identity directory holds %d entries", len(entries))
	}
	if read(t, h.ssh("known_hosts")) != "" {
		t.Fatal("a removed connection's host key is still pinned")
	}
}

func TestTheWebContainerIsRestartedOnlyForItsOwnEnvironment(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	// Not installed: nothing to restart, and that is not a failure.
	if result := h.ok(); result.Restarted || h.web.restarts != 0 || !result.WebChanged {
		t.Fatalf("result: %+v", result)
	}
	// Slow to come back, but healthy within the bound.
	h.web.installed = true
	h.web.health = []string{"starting", "starting", "healthy"}
	rotate := func() {
		h.fake.Edit(func(fake *connecttest.Fake) {
			value := "rotated-" + time.Now().String()
			fake.Items[len(fake.Items)-1].Fields[1].Value = &value
		})
	}
	rotate()
	if result := h.ok(); !result.Restarted || h.web.restarts != 1 || h.web.asked != 3 {
		t.Fatalf("result: %+v after %d health reads", result, h.web.asked)
	}
	// Never healthy: the run fails, after a bounded wait, with the files installed.
	h.web.health, h.web.asked = []string{"unhealthy"}, 0
	rotate()
	_, err := h.run()
	if !errors.Is(err, ErrUnhealthy) || Class(err) != "web_unhealthy" || h.web.asked != 12 || h.web.restarts != 2 {
		t.Fatalf("an unhealthy restart: %v after %d health reads", err, h.web.asked)
	}
	if status := h.status(); status.LastAttempt.Failure != "web_unhealthy" {
		t.Fatalf("status: %+v", status.LastAttempt)
	}
}

// rotate changes one application variable in the vault.
func (h *host) rotate() {
	h.fake.Edit(func(fake *connecttest.Fake) {
		value := "rotated-" + strconv.Itoa(fake.ContentVersion)
		fake.Items[len(fake.Items)-1].Fields[1].Value = &value
	})
}

func (h *host) owed() bool {
	_, err := os.Lstat(filepath.Join(h.layout().RuntimeDir, pendingName))
	return err == nil
}

// A rotated application secret must reach the running container. Whatever
// stops a run after the environment file is rewritten, the restart is still
// made: in that run if it can be, otherwise by the next.
func TestAFailureAfterTheEnvironmentIsWrittenStillRestartsTheContainer(t *testing.T) {
	for _, step := range []string{"web-env", "controller", "state"} {
		t.Run("failed at "+step, func(t *testing.T) {
			h := newHost(t, apiToken(1, "example", "EXAMPLE"))
			h.web.installed = true
			h.ok()
			h.rotate()
			h.runner.fault = func(at string) error {
				if at == step {
					return errors.New("injected failure")
				}
				return nil
			}
			result, err := h.run()
			if err == nil || !result.Restarted || h.web.restarts != 2 || h.owed() {
				t.Fatalf("the failing run left the container on the old environment: %v %+v restarts=%d", err, result, h.web.restarts)
			}
			h.runner.fault = nil
			next := h.ok()
			if next.Restarted || h.web.restarts != 2 || h.owed() {
				t.Fatalf("the next run restarted again: %+v", next)
			}
			if _, err := connections.ReadFile(h.document(), os.Getuid()); err != nil {
				t.Fatalf("the next run did not finish the install: %v", err)
			}
			if h.ok().Outcome != "current" {
				t.Fatal("the host did not settle")
			}
		})
		// Killed rather than failed: nothing more can be done in that run.
		t.Run("terminated at "+step, func(t *testing.T) {
			h := newHost(t, apiToken(1, "example", "EXAMPLE"))
			h.web.installed = true
			h.ok()
			h.rotate()
			ctx, cancel := context.WithCancel(context.Background())
			h.runner.fault = func(at string) error {
				if at == step {
					cancel()
					return errors.New("terminated")
				}
				return nil
			}
			if _, err := h.runner.Run(ctx); err == nil || h.web.restarts != 1 || !h.owed() {
				t.Fatalf("a terminated run: %v restarts=%d owed=%v", err, h.web.restarts, h.owed())
			}
			rotated := read(t, h.appEnv())
			h.runner.fault = nil
			h.log.Reset()
			next := h.ok()
			// The file is already equal, so nothing "changed"; the restart is
			// owed all the same, on the skip path too.
			if next.WebChanged || !next.Restarted || h.web.restarts != 2 || h.owed() || read(t, h.appEnv()) != rotated {
				t.Fatalf("the restart was lost: %+v restarts=%d\n%s", next, h.web.restarts, h.log)
			}
			if step == "state" && next.Outcome != "current" {
				t.Fatalf("after the state was saved the next run reads the vault again: %+v", next)
			}
			if after := h.ok(); after.Restarted || h.web.restarts != 2 {
				t.Fatalf("a settled host restarted again: %+v", after)
			}
		})
	}
}

func TestARestartIsNeverMadeOntoAFileThatWasNotWritten(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	h.web.installed = true
	h.ok()
	before := read(t, h.appEnv())
	h.rotate()
	// Stopped after the mark and before the write.
	h.runner.fault = func(at string) error {
		if at == "marked" {
			return errors.New("injected failure")
		}
		return nil
	}
	if result, err := h.run(); err == nil || result.Restarted || h.web.restarts != 1 || read(t, h.appEnv()) != before || !h.owed() {
		t.Fatalf("restarted onto an unwritten environment: %v %+v", err, result)
	}
	// Cut short by the write itself: the mark names another file.
	os.Chmod(h.appEnv(), 0o600)
	os.WriteFile(h.appEnv(), []byte("DJANGO_SECRET_KEY='cut"), 0o400)
	os.Chmod(h.appEnv(), 0o400)
	h.runner.fault = func(string) error { return errors.New("injected failure") }
	if result, _ := h.run(); result.Restarted || h.web.restarts != 1 {
		t.Fatal("restarted onto a cut file")
	}
	h.runner.fault = nil
	if result := h.ok(); !result.WebChanged || !result.Restarted || h.web.restarts != 2 || h.owed() || read(t, h.appEnv()) == before {
		t.Fatalf("the next run did not write and restart: %+v", result)
	}
	// An unchanged render with nothing owed leaves no mark.
	h.fake.Edit(func(*connecttest.Fake) {})
	if result := h.ok(); result.Restarted || h.owed() {
		t.Fatalf("an unchanged environment marked a restart: %+v", result)
	}
}

func TestAnUnhealthyRestartIsRetriedUntilItIsHealthy(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	h.web.installed = true
	h.ok()
	h.rotate()
	h.web.health = []string{"unhealthy"}
	if _, err := h.run(); Class(err) != "web_unhealthy" || !h.owed() {
		t.Fatalf("an unhealthy restart: %v", err)
	}
	// The vault is unchanged and every file is intact, yet the run is not a
	// success while the container has not come back on the new environment.
	if _, err := h.run(); Class(err) != "web_unhealthy" || h.web.restarts != 3 || !h.owed() {
		t.Fatalf("the restart was not retried: %v restarts=%d", err, h.web.restarts)
	}
	if status := h.status(); status.LastAttempt.Outcome != "failed" || status.LastAttempt.Failure != "web_unhealthy" {
		t.Fatalf("status: %+v", status.LastAttempt)
	}
	// Connect down does not stand in the way of it either.
	h.web.health = []string{"healthy"}
	h.runner.Config.ConnectTimeout = 100 * time.Millisecond
	h.fake.Unavailable = 1 << 30
	if result, err := h.run(); Class(err) != "connect_unavailable" || !result.Restarted || h.web.restarts != 4 || h.owed() {
		t.Fatalf("the owed restart waited for Connect: %v %+v", err, result)
	}
	h.fake.Unavailable = 0
	h.runner.Config.ConnectTimeout = 5 * time.Second
	if result := h.ok(); result.Restarted || h.web.restarts != 4 {
		t.Fatalf("a healthy container was restarted again: %+v", result)
	}
}

func TestAnUnreadableRestartMarkForcesAFullRender(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	h.web.installed = true
	h.ok()
	os.WriteFile(filepath.Join(h.layout().RuntimeDir, pendingName), []byte("not a mark"), 0o600)
	if result := h.ok(); result.Outcome != "rendered" || !result.Restarted || h.web.restarts != 2 || h.owed() {
		t.Fatalf("an unreadable mark was skipped over: %+v", result)
	}
	// No container: nothing to restart, and one created later reads the file.
	h.web.installed = false
	h.rotate()
	if result := h.ok(); result.Restarted || h.owed() || h.web.restarts != 2 {
		t.Fatalf("a host without the container kept a restart owed: %+v", result)
	}
}

// The launcher copies the environment under the shared lock; the renderer
// must not rewrite it, or anything else, while a launcher holds that lock.
func TestNothingInstalledIsTouchedWhileALauncherHoldsTheLock(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	h.web.installed = true
	h.ok()
	paths := []string{h.appEnv(), h.document()}
	before := []string{read(t, paths[0]), read(t, paths[1])}
	inodes := []uint64{inode(t, paths[0]), inode(t, paths[1])}
	launcher, err := os.OpenFile(filepath.Join(h.layout().RuntimeDir, "ssh.lock"), os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer launcher.Close()
	if err := syscall.Flock(int(launcher.Fd()), syscall.LOCK_SH); err != nil {
		t.Fatal(err)
	}
	h.rotate()
	h.fake.Edit(func(fake *connecttest.Fake) {
		rotated := secretToken + "-rotated"
		fake.Items[0].Fields[3].Value = &rotated
	})
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	if _, err := h.runner.Run(ctx); Class(err) != "host" {
		t.Fatalf("the renderer went ahead under a launcher's lock: %v", err)
	}
	for index, path := range paths {
		if read(t, path) != before[index] || inode(t, path) != inodes[index] {
			t.Fatalf("%s was rewritten while a launcher could be copying it", filepath.Base(path))
		}
	}
	if h.owed() || h.web.restarts != 1 {
		t.Fatal("a run that wrote nothing owes or made a restart")
	}
	syscall.Flock(int(launcher.Fd()), syscall.LOCK_UN)
	if result := h.ok(); !result.WebChanged || !result.Restarted {
		t.Fatalf("after the launcher let go: %+v", result)
	}
}

func TestConcurrentRunsDoNotInterleave(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	h.fake.Intercept = func(_ http.ResponseWriter, r *http.Request) bool {
		if strings.HasSuffix(r.URL.Path, "/items") {
			once.Do(func() {
				h.fake.Unlock()
				close(entered)
				<-release
				h.fake.Lock()
			})
		}
		return false
	}
	first := make(chan error, 1)
	go func() {
		_, err := h.runner.Run(context.Background())
		first <- err
	}()
	<-entered
	// A second renderer, as a second process would be: its own runner.
	second := *h.runner
	var log bytes.Buffer
	second.Log = slog.New(slog.NewTextHandler(&log, nil))
	if _, err := second.Run(context.Background()); !errors.Is(err, install.ErrBusy) || Class(err) != "busy" {
		t.Fatalf("a second renderer ran beside the first: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(h.layout().RuntimeDir, StatusName)); err == nil {
		t.Fatal("the refused renderer wrote the status document under the running one")
	}
	if len(h.staging()) != 0 {
		t.Fatal("the refused renderer touched staging")
	}
	close(release)
	if err := <-first; err != nil {
		t.Fatalf("the first renderer failed: %v", err)
	}
	if h.ok().Outcome != "current" {
		t.Fatal("the lock outlived its run")
	}
}

func TestAVaultEditedMidReadIsReadAgain(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	items := "/v1/vaults/" + connecttest.VaultID + "/items/"
	edits := 1
	h.fake.Intercept = func(_ http.ResponseWriter, r *http.Request) bool {
		if strings.HasPrefix(r.URL.Path, items) && edits > 0 {
			edits--
			h.fake.ContentVersion++
		}
		return false
	}
	h.ok()
	if listings := h.fake.Count("/v1/vaults/" + connecttest.VaultID + "/items"); listings < 4 {
		t.Fatalf("the vault was not read a second time: %d item requests", listings)
	}
	if status := h.status(); *status.LastSuccess.ContentVersion != 2 {
		t.Fatalf("the recorded version is not the one read: %d", *status.LastSuccess.ContentVersion)
	}
	// One that never holds still is refused rather than installed half and half.
	h.fake.Edit(func(*connecttest.Fake) {})
	edits = 1 << 30
	before := read(t, h.document())
	if _, err := h.run(); !errors.Is(err, ErrChanging) || Class(err) != "connect_unavailable" {
		t.Fatalf("a vault that kept changing was installed: %v", err)
	}
	if read(t, h.document()) != before {
		t.Fatal("a mixed read was installed")
	}
	// An item whose version moved between the listing and its read is the same.
	edits = 0
	h.fake.Intercept = func(http.ResponseWriter, *http.Request) bool { return false }
	stale := newHost(t, apiToken(1, "example", "EXAMPLE"))
	stale.fake.Intercept = func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Path == items+id(1) {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"id":"` + id(1) + `","version":99,"category":"LOGIN","vault":{"id":"` + connecttest.VaultID + `"}}`))
			return true
		}
		return false
	}
	if _, err := stale.run(); !errors.Is(err, ErrChanging) {
		t.Fatalf("an item newer than its listing was accepted: %v", err)
	}
}

func TestRetiredItemsAreNotRendered(t *testing.T) {
	archived := apiToken(2, "retired", "RETIRED")
	archived.State = "ARCHIVED"
	deleted := apiToken(3, "deleted", "DELETED")
	deleted.State = "DELETED"
	h := newHost(t, apiToken(1, "example", "EXAMPLE"), archived, deleted)
	h.ok()
	document, err := connections.ReadFile(h.document(), os.Getuid())
	if err != nil || len(document.Connections) != 1 || document.Connections[0].Ref != "example" {
		t.Fatalf("an archived or deleted connection was rendered: %v", err)
	}
	if h.fake.Count("/v1/vaults/"+connecttest.VaultID+"/items/"+id(2)) != 0 {
		t.Fatal("an archived item was read")
	}
}

func TestTheVaultMustResolveUniquely(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	h.fake.ExtraVaults = []map[string]any{{"id": "wwwwwwwwwwwwwwwwwwwwwwwwww", "name": connecttest.VaultName}}
	if _, err := h.run(); Class(err) != "connect_response" {
		t.Fatalf("an ambiguous vault name was accepted: %v", err)
	}
	// By identifier it is one vault again.
	h.runner.Config.Vault = connecttest.VaultID
	h.ok()
	if document, _ := connections.ReadFile(h.document(), os.Getuid()); document.Entries()["EXAMPLE_STORE_VAULT"] != connecttest.VaultID {
		t.Fatal("the store vault is not the vault as the host names it")
	}
	h.fake.ExtraVaults = []map[string]any{{"id": "not-an-identifier", "name": "Another"}}
	h.runner.Config.Vault = "Another"
	if _, err := h.run(); Class(err) != "connect_response" {
		t.Fatalf("a vault with a malformed identifier was accepted: %v", err)
	}
}

func TestConnectIsUnlockedByTheAuthenticatedRequestAndWaitedFor(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	h.fake.Unavailable = 4
	h.fake.SyncStatus = "TOKEN_NEEDED"
	h.ok()
	if !strings.Contains(h.log.String(), "attempts=5") {
		t.Fatalf("the retries were not reported: %s", h.log)
	}
	if h.fake.Requests[0] != "/v1/vaults" || h.fake.Authorizations[0] != "Bearer "+connecttest.Token {
		t.Fatalf("the first request was not the authenticated listing: %v", h.fake.Requests[:1])
	}
	if status := h.status(); status.Connect.Dependencies[0] != (Dependency{Service: "sync", Status: "TOKEN_NEEDED"}) {
		t.Fatalf("sync state was not recorded: %+v", status.Connect)
	}
	// What an unauthenticated endpoint says is not copied into the document.
	h.fake.Edit(func(*connecttest.Fake) {})
	h.fake.SyncStatus = "<script>" + secretToken + "</script>"
	h.ok()
	if status := h.status(); status.Connect.Dependencies[0].Status != "unreadable" {
		t.Fatalf("free text from /health reached the status document: %+v", status.Connect)
	}
}

func TestTheWritersTokenReachesTheDocumentByProjection(t *testing.T) {
	writer := item{ID: id(2), Version: 1, Fields: []field{
		f("connection_ref", "publisher"), f("projection", "service_account"), f("env_prefix", "ONEPASSWORD"),
		f("provider", "onepassword"), b("credential", secretWriter)}}
	h := newHost(t, apiToken(1, "example", "EXAMPLE"), writer)
	h.ok()
	document, _ := connections.ReadFile(h.document(), os.Getuid())
	if document.Entries()["ONEPASSWORD_API_TOKEN"] != secretWriter || document.Entries()["ONEPASSWORD_PROVIDER"] != "onepassword" {
		t.Fatal("the writer's token did not reach the connections document")
	}
	if h.ok().Outcome != "current" {
		t.Fatal("the second run was not current")
	}
	// Rotated in the vault: rendered, to the controller only.
	h.fake.Edit(func(fake *connecttest.Fake) {
		rotated := secretWriter + "-rotated"
		fake.Items[1].Fields[4].Value = &rotated
		fake.Items[1].Version++
	})
	if result := h.ok(); result.Outcome != "rendered" || !result.Changed || result.WebChanged {
		t.Fatalf("a rotated token was not rendered: %+v", result)
	}
	// The reader token itself is never part of what is rendered.
	if strings.Contains(read(t, h.document()), connecttest.Token) {
		t.Fatal("the reader token is in the connections document")
	}
}

func TestClassesAreDistinct(t *testing.T) {
	for class, err := range map[string]error{
		"config": ErrConfig, "busy": install.ErrBusy, "host": install.ErrHost, "connect_denied": connect.ErrDenied,
		"connect_unavailable": connect.ErrUnavailable, "connect_response": connect.ErrResponse, "content": project.ErrContent,
		"web_unhealthy": ErrUnhealthy, "internal": errors.New("anything else"),
	} {
		if Class(err) != class {
			t.Errorf("%v is filed under %q, not %q", err, Class(err), class)
		}
	}
	if Class(nil) != "" || Class(connect.ErrRedirect) != "connect_response" || Class(connect.ErrNotLoopback) != "connect_response" ||
		Class(connect.ErrEndpoint) != "config" || Class(project.ErrRegistry) != "config" {
		t.Fatal("classes")
	}
}

func TestDockerWebDrivesTheContainerByFixedArguments(t *testing.T) {
	dir := t.TempDir()
	script := `#!/bin/sh
printf '%s\n' "$*" >> "` + dir + `/calls"
env > "` + dir + `/env"
case "$1" in
    inspect) [ "$2" = --format ] && echo healthy; exit 0 ;;
    restart) exit 0 ;;
esac
exit 1
`
	if err := os.WriteFile(filepath.Join(dir, "docker"), []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+":"+os.Getenv("PATH"))
	t.Setenv("CREDENTIALS_DIRECTORY", "/run/credentials/example")
	t.Setenv("SEVERINO_SECRETS_VAULT", connecttest.VaultName)
	web := DockerWeb{Container: "severino-hq"}
	if !web.Installed(context.Background()) || web.Restart(context.Background()) != nil || web.Health(context.Background()) != "healthy" {
		t.Fatal("the container was not driven")
	}
	if calls := read(t, filepath.Join(dir, "calls")); calls != "inspect --type container severino-hq\nrestart severino-hq\ninspect --format {{.State.Health.Status}} severino-hq\n" {
		t.Fatalf("docker was called with: %s", calls)
	}
	if env := read(t, filepath.Join(dir, "env")); strings.Contains(env, "CREDENTIALS_DIRECTORY") || strings.Contains(env, "SEVERINO_") {
		t.Fatalf("the child inherited the renderer's environment: %s", env)
	}
	t.Setenv("PATH", t.TempDir())
	if web.Installed(context.Background()) || web.Health(context.Background()) != "" {
		t.Fatal("a host without docker reported a container")
	}
}
