package runtime

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
)

func TestConnectionResolutionRefusesAmbiguity(t *testing.T) {
	env := Environment{"A_CONNECTION_REF": "example-a", "A_PROVIDER": "example", "B_CONNECTION_REF": "example-b", "B_PROVIDER": "example"}
	if _, err := env.Prefix("example", ""); !errors.Is(err, ErrAmbiguousConnection) {
		t.Fatalf("ambiguous provider accepted: %v", err)
	}
	if prefix, err := env.Prefix("example", "example-b"); err != nil || prefix != "B" {
		t.Fatalf("%s %v", prefix, err)
	}
	if _, err := env.Prefix("example", "absent"); !errors.Is(err, ErrNoSuchConnection) {
		t.Fatalf("unknown connection accepted: %v", err)
	}
}
func TestOnlyExplicitManagementDeclarationAllowsWrites(t *testing.T) {
	env := Environment{"A_CONNECTION_REF": "example"}
	for _, value := range []string{"", "false", "0", "read", "TRUE", "yes", "1"} {
		env["A_MANAGES"] = value
		want := value == "TRUE" || value == "yes" || value == "1"
		if env.Manages("example") != want {
			t.Fatalf("value=%q", value)
		}
	}
	if env.Manages("unknown") {
		t.Fatal("unknown connection manages")
	}
}
func TestSSHRejectsDestinationOptionsAndMalformedPorts(t *testing.T) {
	env := Environment{"A_CONNECTION_REF": "example", "A_HOST": "host.example", "A_USER": "reader", "A_PORT": "22", "A_HOST_KEY": "ssh-ed25519 example"}
	for field, values := range map[string][]string{
		"A_HOST": {"-oProxyCommand=x", "host other", "user@host", ""},
		"A_USER": {"-oProxyCommand=x", "root@host", "root user", ""},
		"A_PORT": {"0", "65536", "x", "-22"},
	} {
		previous := env[field]
		for _, value := range values {
			env[field] = value
			if _, err := env.SSH("example"); err == nil || !errors.As(err, new(*ProviderError)) {
				t.Errorf("accepted %s=%q: %v", field, value, err)
			}
		}
		env[field] = previous
	}
	if _, err := env.SSH("example"); err != nil {
		t.Fatal(err)
	}
}
func TestMissingSettingDoesNotExposeEnvironment(t *testing.T) {
	_, err := (Environment{}).Required("PRIVATE_PROVIDER", "TOKEN")
	if !errors.Is(err, ErrSettingMissing) || strings.Contains(err.Error(), "PRIVATE_PROVIDER") {
		t.Fatal(err)
	}
}

// Naming a connection does not skip the provider check: one vendor's
// credential is never sent to another.
func TestNamedConnectionMustBeTheProvidersOwn(t *testing.T) {
	env := Environment{"CLOUDFLARE_DNS_CONNECTION_REF": "dns", "PORTAINER_HOME_CONNECTION_REF": "home", "PORTAINER_HOME_PROVIDER": "portainer"}
	for _, provider := range []ConnectionProvider{ConnectionProviderOnePassword, ConnectionProviderPortainer, ConnectionProviderTailscale} {
		if _, err := env.Prefix(provider, "dns"); !errors.Is(err, ErrForeignConnection) {
			t.Errorf("%s took dns: %v", provider, err)
		}
	}
	if prefix, err := env.Prefix("portainer", "home"); err != nil || prefix != "PORTAINER_HOME" {
		t.Fatalf("own ref: %q %v", prefix, err)
	}
}

func TestSharedRefNamesNoConnection(t *testing.T) {
	env := Environment{"NPM_CONNECTION_REF": "proxy", "NPM_HOME_CONNECTION_REF": "proxy"}
	if _, ok := env.Prefixes()["proxy"]; ok {
		t.Fatal("a shared ref resolved")
	}
	if _, err := env.Prefix("npm", "proxy"); !errors.Is(err, ErrNoSuchConnection) {
		t.Fatalf("a shared ref was usable: %v", err)
	}
}

func writeConnections(t *testing.T, document connections.Document, mode os.FileMode) string {
	t.Helper()
	data, err := document.Encode()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "connections.json")
	if err := os.WriteFile(path, data, mode); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestConnectionsArriveInTheDocumentNotTheEnvironment(t *testing.T) {
	document := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{
		{Ref: "example-a", Prefix: "A", Values: map[string]string{"CONNECTION_REF": "example-a", "PROVIDER": "example", "API_TOKEN": "example-token", "MANAGES": "true"}},
		{Ref: "edge", Prefix: "EDGE", Values: map[string]string{"CONNECTION_REF": "edge", "HOST": "edge.example.com", "PORT": "2222", "USER": "deploy", "HOST_KEY": "ssh-ed25519 example", "ROLE": "edge"}},
	}}
	path := writeConnections(t, document, 0o400)
	env, err := LoadEnvironment([]string{"PATH=/bin", ConnectionsFile + "=" + path, "HQ_IN_PROCESS=1"})
	if err != nil {
		t.Fatal(err)
	}
	// The method surface providers read through is unchanged.
	if prefix, err := env.Prefix("example", "example-a"); err != nil || prefix != "A" {
		t.Fatalf("%s %v", prefix, err)
	}
	if token, err := env.Required("A", "API_TOKEN"); err != nil || token != "example-token" {
		t.Fatalf("token: %v", err)
	}
	if !env.Manages("example-a") || env.Provider("example-a") != "example" || !slices.Equal(env.Refs("example"), []string{"example-a"}) {
		t.Fatal("the connection did not resolve")
	}
	if target, err := env.SSH("edge"); err != nil || target.Host != "edge.example.com" || target.Port != 2222 || !slices.Equal(env.RoleRefs("edge"), []string{"edge"}) {
		t.Fatalf("ssh: %+v %v", target, err)
	}
	// HQ's own process gets neither the connections nor the way to them.
	for _, entry := range env.WithoutConnections() {
		if strings.HasPrefix(entry, "A_") || strings.HasPrefix(entry, "EDGE_") || strings.HasPrefix(entry, ConnectionsFile+"=") {
			t.Fatalf("the bridge would inherit %s", strings.SplitN(entry, "=", 2)[0])
		}
	}
	if !slices.Contains(env.WithoutConnections(), "HQ_IN_PROCESS=1") {
		t.Fatal("the bridge lost its own settings")
	}
	// No document named: the environment alone, as a local run has it.
	env, err = LoadEnvironment([]string{"PATH=/bin"})
	if err != nil || len(env) != 1 {
		t.Fatalf("an environment without a document: %v", err)
	}
}

func TestAnUnsafeOrAmbiguousConnectionsDocumentIsRefused(t *testing.T) {
	document := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{
		{Ref: "example", Prefix: "A", Values: map[string]string{"CONNECTION_REF": "example", "API_TOKEN": "sentinel-token-value"}}}}
	refused := func(why string, entries []string) {
		t.Helper()
		env, err := LoadEnvironment(entries)
		if err == nil || env != nil {
			t.Fatalf("%s was accepted", why)
		}
		if strings.Contains(err.Error(), "sentinel-token-value") {
			t.Fatalf("%s: the refusal carries a value: %v", why, err)
		}
	}
	private := writeConnections(t, document, 0o400)
	refused("a missing document", []string{ConnectionsFile + "=" + private + ".absent"})
	refused("a document readable by others", []string{ConnectionsFile + "=" + writeConnections(t, document, 0o444)})
	link := filepath.Join(t.TempDir(), "link.json")
	if err := os.Symlink(private, link); err != nil {
		t.Fatal(err)
	}
	refused("a linked document", []string{ConnectionsFile + "=" + link})
	// A setting the launcher already passed is not silently replaced.
	refused("a collision with the environment", []string{ConnectionsFile + "=" + private, "A_API_TOKEN=from-the-environment"})
	// Nor is a connection in the environment accepted beside the document.
	refused("a connection in the environment", []string{ConnectionsFile + "=" + private, "B_CONNECTION_REF=other", "B_API_TOKEN=x"})
	garbage := filepath.Join(t.TempDir(), "garbage.json")
	if err := os.WriteFile(garbage, []byte(`{"schema_version":2,"connections":[]}`), 0o400); err != nil {
		t.Fatal(err)
	}
	refused("a wrong schema version", []string{ConnectionsFile + "=" + garbage})
	if err := os.WriteFile(garbage, []byte(`A_API_TOKEN='sentinel-token-value'`), 0o600); err != nil {
		os.Chmod(garbage, 0o600)
		os.WriteFile(garbage, []byte(`A_API_TOKEN='sentinel-token-value'`), 0o600)
	}
	refused("a shell environment file", []string{ConnectionsFile + "=" + garbage})
}
