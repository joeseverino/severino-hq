package runtime

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
)

var kept = connections.Store{Vault: "example-vault", Item: "example-item"}

func tokenConnection(ref, provider string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: provider, Store: kept, APIToken: &connections.APIToken{APIToken: "example-token", URL: "https://api.example.com"}}
}

func transport(ref string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: "ssh", Store: kept, SSHTransport: &connections.SSHTransport{
		Host: "host.example", User: "reader", Port: "22", HostKey: "ssh-ed25519 example", Identity: "example identity"}}
}

func TestConnectionResolutionRefusesAmbiguity(t *testing.T) {
	held := NewConnections(tokenConnection("example-a", "example"), tokenConnection("example-b", "example"))
	if _, err := held.For("example", ""); !errors.Is(err, ErrAmbiguousConnection) {
		t.Fatalf("ambiguous provider accepted: %v", err)
	}
	if connection, err := held.For("example", "example-b"); err != nil || connection.Ref != "example-b" {
		t.Fatalf("%s %v", connection, err)
	}
	if _, err := held.For("example", "absent"); !errors.Is(err, ErrNoSuchConnection) {
		t.Fatalf("unknown connection accepted: %v", err)
	}
	if !slices.Equal(held.Refs("example"), []string{"example-a", "example-b"}) || len(held.Refs("other")) != 0 {
		t.Fatalf("refs: %v", held.Refs("example"))
	}
}

func TestOnlyExplicitManagementDeclarationAllowsWrites(t *testing.T) {
	reader, writer := tokenConnection("reader", "example"), tokenConnection("writer", "example")
	writer.Manages = true
	held := NewConnections(reader, writer)
	if held.Manages("reader") || !held.Manages("writer") {
		t.Fatal("manages is not the connection's own declaration")
	}
	if held.Manages("unknown") {
		t.Fatal("unknown connection manages")
	}
}

func TestSSHRejectsDestinationOptionsAndMalformedPorts(t *testing.T) {
	for field, values := range map[string][]string{
		"host": {"-oProxyCommand=x", "host other", "user@host", ""},
		"user": {"-oProxyCommand=x", "root@host", "root user", ""},
		"port": {"0", "65536", "x", "-22", ""},
	} {
		for _, value := range values {
			connection := transport("example")
			switch field {
			case "host":
				connection.SSHTransport.Host = value
			case "user":
				connection.SSHTransport.User = value
			case "port":
				connection.SSHTransport.Port = value
			}
			if _, err := NewConnections(connection).SSH("example"); err == nil || !errors.As(err, new(*ProviderError)) {
				t.Errorf("accepted %s=%q: %v", field, value, err)
			}
		}
	}
	held := NewConnections(transport("example"), tokenConnection("api", "example"))
	if target, err := held.SSH("example"); err != nil || target.Host != "host.example" || target.Port != 22 || target.User != "reader" {
		t.Fatalf("%+v %v", target, err)
	}
	if _, err := held.SSH("api"); !errors.Is(err, ErrNoSuchConnection) {
		t.Fatalf("a connection that is no transport gave an SSH target: %v", err)
	}
	if !slices.Equal(held.SSHRefs(), []string{"example"}) {
		t.Fatalf("ssh refs: %v", held.SSHRefs())
	}
}

func TestMissingSettingDoesNotNameIt(t *testing.T) {
	_, err := NewConnections().For("private_provider", "")
	if !errors.Is(err, ErrSettingMissing) || strings.Contains(err.Error(), "private_provider") {
		t.Fatal(err)
	}
	// A connection that arrived in another shape lacks every setting of this one.
	connection, err := NewConnections(tokenConnection("example", "example")).For("example", "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Need(connection.Login); !errors.Is(err, ErrSettingMissing) {
		t.Fatalf("a token was read as a login: %v", err)
	}
	if settings, err := Need(connection.APIToken); err != nil || settings.APIToken != "example-token" {
		t.Fatalf("its own shape: %v", err)
	}
}

// A connection whose shape requires a setting it holds only space for is not
// usable, and the refusal names neither the setting nor its value.
func TestAConnectionMissingARequiredSettingIsNotUsable(t *testing.T) {
	blank := tokenConnection("example", "example")
	blank.APIToken.APIToken = "   "
	if _, err := NewConnections(blank).For("example", "example"); !errors.Is(err, ErrSettingMissing) {
		t.Fatalf("a connection with a blank token was usable: %v", err)
	}
}

func TestSettingsAreTrimmedOnce(t *testing.T) {
	spaced := tokenConnection("example", " example ")
	spaced.APIToken.URL = " https://api.example.com/ "
	connection, err := NewConnections(spaced).For("example", "example")
	if err != nil || connection.APIToken.URL != "https://api.example.com/" {
		t.Fatalf("%v", err)
	}
}

// Naming a connection does not skip the provider check: one vendor's
// credential is never sent to another.
func TestNamedConnectionMustBeTheProvidersOwn(t *testing.T) {
	held := NewConnections(tokenConnection("dns", "cloudflare_dns"), tokenConnection("home", "portainer"))
	for _, provider := range []ConnectionProvider{ConnectionProviderOnePassword, ConnectionProviderPortainer, ConnectionProviderTailscale} {
		if _, err := held.For(provider, "dns"); !errors.Is(err, ErrForeignConnection) {
			t.Errorf("%s took dns: %v", provider, err)
		}
	}
	if connection, err := held.For("portainer", "home"); err != nil || connection.Ref != "home" {
		t.Fatalf("own ref: %s %v", connection, err)
	}
}

func TestSharedRefNamesNoConnection(t *testing.T) {
	held := NewConnections(tokenConnection("proxy", "npm"), tokenConnection("proxy", "npm"))
	if _, ok := held.Get("proxy"); ok {
		t.Fatal("a shared ref resolved")
	}
	if _, err := held.For("npm", "proxy"); !errors.Is(err, ErrNoSuchConnection) {
		t.Fatalf("a shared ref was usable: %v", err)
	}
}

func TestRolesAreTheTransportsOwn(t *testing.T) {
	edge, plain := transport("edge"), transport("plain")
	edge.SSHTransport.Role = "edge"
	held := NewConnections(edge, plain)
	if !slices.Equal(held.RoleRefs("edge"), []string{"edge"}) || len(held.RoleRefs("other")) != 0 {
		t.Fatalf("role refs: %v", held.RoleRefs("edge"))
	}
}

// The settings of a shape no provider owns come from the one connection that
// arrived in it.
func TestOnlyFindsTheOneConnectionOfAShape(t *testing.T) {
	account := func(ref string) connections.Connection {
		return connections.Connection{Ref: ref, Provider: "acme", Store: kept, ACME: &connections.ACME{DirectoryURL: "https://acme.example.com/directory", Email: "ops@example.com"}}
	}
	pick := func(c connections.Connection) *connections.ACME { return c.ACME }
	if _, err := Only(NewConnections(tokenConnection("example", "example")), pick); !errors.Is(err, ErrSettingMissing) {
		t.Fatalf("no account: %v", err)
	}
	if settings, err := Only(NewConnections(account("acme"), tokenConnection("example", "example")), pick); err != nil || settings.Email != "ops@example.com" {
		t.Fatalf("one account: %v", err)
	}
	if _, err := Only(NewConnections(account("acme"), account("other")), pick); !errors.Is(err, ErrAmbiguousConnection) {
		t.Fatalf("two accounts: %v", err)
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

func load(entries []string) (Connections, error) {
	env, err := ReadEnvironment(entries)
	if err != nil {
		return Connections{}, err
	}
	return LoadConnections(env)
}

func TestConnectionsArriveInTheDocumentNotTheEnvironment(t *testing.T) {
	managed := tokenConnection("example-a", "example")
	managed.Manages = true
	edge := connections.Connection{Ref: "edge", Provider: "ssh", Store: kept, SSHTransport: &connections.SSHTransport{
		Host: "edge.example.com", Port: "2222", User: "deploy", HostKey: "ssh-ed25519 example", Identity: "example identity", Role: "edge"}}
	document := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{managed, edge}}
	path := writeConnections(t, document, 0o400)
	held, err := load([]string{"PATH=/bin", "HQ_CONTROLLER_CONNECTIONS=" + path})
	if err != nil {
		t.Fatal(err)
	}
	connection, err := held.For("example", "example-a")
	if err != nil || connection.APIToken == nil || connection.APIToken.APIToken != "example-token" {
		t.Fatalf("token: %v", err)
	}
	if !held.Manages("example-a") || !slices.Equal(held.Refs("example"), []string{"example-a"}) {
		t.Fatal("the connection did not resolve")
	}
	if target, err := held.SSH("edge"); err != nil || target.Host != "edge.example.com" || target.Port != 2222 || !slices.Equal(held.RoleRefs("edge"), []string{"edge"}) {
		t.Fatalf("ssh: %+v %v", target, err)
	}
	// No document named: no connection, as a run given none has it.
	held, err = load([]string{"PATH=/bin"})
	if err != nil || len(held.All()) != 0 {
		t.Fatalf("an environment without a document: %v", err)
	}
}

func TestAnUnsafeOrAmbiguousConnectionsDocumentIsRefused(t *testing.T) {
	secret := tokenConnection("example", "example")
	secret.APIToken.APIToken = "sentinel-token-value"
	document := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{secret}}
	refused := func(why string, entries []string) {
		t.Helper()
		held, err := load(entries)
		if err == nil || len(held.All()) != 0 {
			t.Fatalf("%s was accepted", why)
		}
		if strings.Contains(err.Error(), "sentinel-token-value") {
			t.Fatalf("%s: the refusal carries a value: %v", why, err)
		}
	}
	const named = "HQ_CONTROLLER_CONNECTIONS="
	private := writeConnections(t, document, 0o400)
	refused("a missing document", []string{named + private + ".absent"})
	refused("a document readable by others", []string{named + writeConnections(t, document, 0o444)})
	link := filepath.Join(t.TempDir(), "link.json")
	if err := os.Symlink(private, link); err != nil {
		t.Fatal(err)
	}
	refused("a linked document", []string{named + link})
	// A connection in the environment is refused, beside a document or alone.
	refused("a connection in the environment", []string{named + private, "B_CONNECTION_REF=other", "B_API_TOKEN=x"})
	refused("a connection in the environment alone", []string{"B_CONNECTION_REF=other"})
	garbage := filepath.Join(t.TempDir(), "garbage.json")
	older := `{"schema_version":1,"connections":[{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a","API_TOKEN":"sentinel-token-value"}}]}`
	if err := os.WriteFile(garbage, []byte(older), 0o400); err != nil {
		t.Fatal(err)
	}
	refused("a document of an older version", []string{named + garbage})
	if err := os.Chmod(garbage, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(garbage, []byte(`A_API_TOKEN='sentinel-token-value'`), 0o600); err != nil {
		t.Fatal(err)
	}
	refused("a shell environment file", []string{named + garbage})
}

// A setting in the environment is not a connection's: nothing reads it, so a
// variable that looks like one changes nothing a provider is handed.
func TestTheEnvironmentHoldsNoConnectionSetting(t *testing.T) {
	env, err := ReadEnvironment([]string{"HQ_CONTROLLER_ID= example ", "NPM_PASSWORD=synthetic", "HQ_ACME_DIR=/acme", "NOT_AN_ENTRY"})
	if err != nil {
		t.Fatal(err)
	}
	if env.ControllerID() != "example" || env.ACMEDir != "/acme" {
		t.Fatalf("%+v", env)
	}
	environment := reflect.TypeFor[Environment]()
	for index := range environment.NumField() {
		field := environment.Field(index)
		if !field.IsExported() {
			continue
		}
		if field.Type.Kind() != reflect.String || field.Tag.Get("env") == "" {
			t.Errorf("%s is not one named fact: a provider could look a setting up in it", field.Name)
		}
	}
	if (Environment{}).ControllerID() == "" {
		t.Skip("this machine has no name")
	}
}

// benchDocument is a document of the size a deployment holds: twenty
// connections across the shapes.
func benchDocument(b *testing.B) string {
	b.Helper()
	list := []connections.Connection{}
	for index := range 20 {
		ref := "example-" + string(rune('a'+index))
		switch index % 4 {
		case 0:
			list = append(list, connections.Connection{Ref: ref, Provider: "adguard", Store: kept, Login: &connections.Login{URL: "https://dns.example.com", Username: "reader", Password: "synthetic"}})
		case 1:
			list = append(list, tokenConnection(ref, "portainer"))
		case 2:
			list = append(list, transport(ref))
		default:
			list = append(list, connections.Connection{Ref: ref, Provider: "tailscale", Store: kept, OAuthClient: &connections.OAuthClient{ClientID: "id", ClientSecret: "synthetic"}})
		}
	}
	data, err := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: list}.Encode()
	if err != nil {
		b.Fatal(err)
	}
	path := filepath.Join(b.TempDir(), "connections.json")
	if err := os.WriteFile(path, data, 0o400); err != nil {
		b.Fatal(err)
	}
	return path
}

// What a pass pays once at its start: the environment and every connection.
func BenchmarkLoadConnections(b *testing.B) {
	entries := []string{"PATH=/bin", "HQ_CONTROLLER_ID=example", "HQ_CONTROLLER_CONNECTIONS=" + benchDocument(b)}
	for b.Loop() {
		if _, err := load(entries); err != nil {
			b.Fatal(err)
		}
	}
}

// What a provider pays each time it asks for its connection.
func BenchmarkConnectionForAProvider(b *testing.B) {
	held, err := load([]string{"HQ_CONTROLLER_CONNECTIONS=" + benchDocument(b)})
	if err != nil {
		b.Fatal(err)
	}
	for b.Loop() {
		connection, err := held.For("adguard", "example-a")
		if err != nil {
			b.Fatal(err)
		}
		login, err := Need(connection.Login)
		if err != nil || login.URL == "" || login.Username == "" || login.Password == "" {
			b.Fatal(err)
		}
	}
}

// A provider asks for its connection on every call it makes. Asking allocates
// nothing: the connection is found by ref and handed over as it was loaded.
func TestAskingForAConnectionAllocatesNothing(t *testing.T) {
	held := NewConnections(tokenConnection("example", "example"), transport("edge"))
	allocated := testing.AllocsPerRun(100, func() {
		connection, err := held.For("example", "example")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := Need(connection.APIToken); err != nil {
			t.Fatal(err)
		}
	})
	if allocated != 0 {
		t.Fatalf("asking for a connection allocates %v times", allocated)
	}
}
