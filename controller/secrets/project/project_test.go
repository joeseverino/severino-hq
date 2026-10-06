package project

import (
	"bytes"
	"crypto/sha256"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"os"
	"strings"
	"testing"

	"golang.org/x/crypto/ssh"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/secrets/connectapi"
	"github.com/joeseverino/severino-hq/controller/secrets/connecttest"
)

type (
	item  = connecttest.Item
	field = connecttest.Field
)

var f, b, id = connecttest.F, connecttest.B, connecttest.ID

const secret = "sentinel-credential-value"

// shipped is the registry the repository carries: HQ's registry emits it, and
// it names no connection.
const shipped = "../../../hq/config/controller-connections.json"

func registry(t testing.TB) Registry {
	t.Helper()
	data, err := os.ReadFile(shipped)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseRegistry(data)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

// envItem is an application environment item with n variables.
func envItem(n int) item {
	fields := []field{}
	for index := range n {
		fields = append(fields, f("EXAMPLE_"+string(rune('A'+index)), "value"))
	}
	return item{ID: id(900), Title: "example env", Fields: fields}
}

func apiToken(n int, ref, prefix string, extra ...field) item {
	return item{ID: id(n), Title: "Example " + ref, Fields: append([]field{
		f("connection_ref", ref), f("projection", "api_token"), f("env_prefix", prefix),
		b("credential", secret), f("website", "https://api.example.com"),
	}, extra...)}
}

func input(t testing.TB, items ...item) Input {
	t.Helper()
	in := Input{
		Registry: registry(t), EnvItem: "example env", MinAppVariables: 15,
		Vault: Vault{Configured: connecttest.VaultName, ID: connecttest.VaultID, Name: connecttest.VaultName},
	}
	for _, each := range append(items, envItem(15)) {
		in.Items = append(in.Items, each.Full())
	}
	return in
}

// refused asserts a refusal that names why and carries no credential.
func refused(t *testing.T, in Input, want string) {
	t.Helper()
	out, err := Project(in)
	if !errors.Is(err, ErrContent) {
		t.Fatalf("accepted (wanted %q): %v", want, err)
	}
	if !strings.Contains(err.Error(), want) {
		t.Fatalf("refused with %q, wanted %q", err, want)
	}
	if strings.Contains(err.Error(), secret) {
		t.Fatalf("the refusal carries a credential: %v", err)
	}
	if out.AppEnv != nil || out.Document.Connections != nil || out.SSH != nil {
		t.Fatal("a refused render still returned output")
	}
}

// entries is a document as one name per fact, for assertions: each connection
// under its ref in capitals, its settings by their registry names beside what
// every connection states of itself.
func entries(t testing.TB, document connections.Document) map[string]string {
	t.Helper()
	found := map[string]string{}
	for _, connection := range document.Connections {
		prefix := strings.ToUpper(strings.NewReplacer("-", "_", ".", "_").Replace(connection.Ref)) + "_"
		found[prefix+RefVariable], found[prefix+ProviderVariable] = connection.Ref, connection.Provider
		found[prefix+StoreVault], found[prefix+StoreItem] = connection.Store.Vault, connection.Store.Item
		if connection.Manages {
			found[prefix+ManagesVariable] = "true"
		}
		if connection.Store.Bootstrap != "" {
			found[prefix+Bootstrap] = connection.Store.Bootstrap
		}
		encoded, err := json.Marshal(connection)
		if err != nil {
			t.Fatal(err)
		}
		var members map[string]json.RawMessage
		if err := json.Unmarshal(encoded, &members); err != nil {
			t.Fatal(err)
		}
		var settings map[string]string
		if err := json.Unmarshal(members[connection.Shape()], &settings); err != nil {
			t.Fatalf("connection %s holds no one shape: %v", connection.Ref, err)
		}
		for name, value := range settings {
			found[prefix+strings.ToUpper(name)] = value
		}
	}
	return found
}

func rendered(t *testing.T, in Input) map[string]string {
	t.Helper()
	out, err := Project(in)
	if err != nil {
		t.Fatal(err)
	}
	return entries(t, out.Document)
}

func TestAnItemDeclaresItsOwnConnection(t *testing.T) {
	values := rendered(t, input(t, apiToken(1, "example", "EXAMPLE")))
	want := map[string]string{
		"EXAMPLE_CONNECTION_REF": "example", "EXAMPLE_API_TOKEN": secret, "EXAMPLE_URL": "https://api.example.com",
		// An item that names no provider is for the one its env_prefix spells.
		"EXAMPLE_PROVIDER": "example",
		// Where the credential is kept: the vault and item it was rendered from.
		"EXAMPLE_STORE_VAULT": connecttest.VaultName, "EXAMPLE_STORE_ITEM": id(1),
	}
	if len(values) != len(want) {
		t.Fatalf("rendered %d variables, wanted %d", len(values), len(want))
	}
	for name, value := range want {
		if values[name] != value {
			t.Errorf("%s was not rendered", name)
		}
	}
}

func TestAnOptionalFieldRendersWhenPresentAndIsNotFatalWhenAbsent(t *testing.T) {
	values := rendered(t, input(t, apiToken(1, "example", "EXAMPLE", f("provider", "portainer"))))
	if values["EXAMPLE_PROVIDER"] != "portainer" {
		t.Fatal("an optional field the item carries was not rendered")
	}
	values = rendered(t, input(t, apiToken(1, "example", "EXAMPLE_API")))
	if values["EXAMPLE_PROVIDER"] != "example_api" || values["EXAMPLE_API_TOKEN"] != secret {
		t.Fatal("an absent optional field changed the render")
	}
	// Present but empty is absent, not an empty provider.
	values = rendered(t, input(t, apiToken(1, "example", "EXAMPLE_API", f("provider", ""))))
	if values["EXAMPLE_PROVIDER"] != "example_api" {
		t.Fatal("an empty optional field was rendered")
	}
}

// Only an explicit yes lets HQ change things through a connection.
func TestManagesIsAnExplicitDeclaration(t *testing.T) {
	for value, want := range map[string]bool{"": false, "false": false, "0": false, "read": false, "no": false,
		"TRUE": true, "yes": true, "1": true, " true ": true} {
		values := rendered(t, input(t, apiToken(1, "example", "EXAMPLE", f("manages", value))))
		if _, manages := values["EXAMPLE_MANAGES"]; manages != want {
			t.Errorf("manages=%q rendered %v", value, manages)
		}
	}
	if _, manages := rendered(t, input(t, apiToken(1, "example", "EXAMPLE")))["EXAMPLE_MANAGES"]; manages {
		t.Fatal("an item that says nothing manages")
	}
}

// A transport that names no provider is a way in to a machine, whatever its
// env_prefix spells; one that names its provider keeps it.
func TestATransportIsForSSHUnlessItSaysOtherwise(t *testing.T) {
	key := connecttest.Ed25519Key(t)
	keyItem := connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, key.Public)
	values := rendered(t, input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"), keyItem))
	if values["EDGE_PROVIDER"] != "ssh" {
		t.Fatalf("a transport that names no provider is for %q", values["EDGE_PROVIDER"])
	}
	values = rendered(t, input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key", f("provider", "example")), keyItem))
	if values["EDGE_PROVIDER"] != "example" {
		t.Fatalf("a transport that names its provider is for %q", values["EDGE_PROVIDER"])
	}
}

func TestAConstantIsRenderedFromTheProjection(t *testing.T) {
	key := connecttest.RSAKey(t)
	keyItem := connecttest.KeyItem(id(2), "Example app key", key.PKCS8, key.Public)
	values := rendered(t, input(t, signingConnection(1, "example", "Example app key"), keyItem))
	if values["EXAMPLE_PROVIDER"] != "github_app" {
		t.Fatal("the constant was not rendered")
	}
	// The item cannot say otherwise: the shape fixes its provider.
	values = rendered(t, input(t, signingConnection(1, "example", "Example app key", f("provider", "other")), keyItem))
	if values["EXAMPLE_PROVIDER"] != "github_app" {
		t.Fatal("an item overrode the provider its shape fixes")
	}
}

func TestAURLIsRenderedByIndex(t *testing.T) {
	login := item{ID: id(1), Fields: []field{
		f("connection_ref", "example"), f("projection", "login"), f("env_prefix", "EXAMPLE"), b("username", "reader"), b("password", secret)},
		URLs: []connecttest.URL{{Href: "https://login.example.com", Primary: true}}}
	if rendered(t, input(t, login))["EXAMPLE_URL"] != "https://login.example.com" {
		t.Fatal("the URL was not rendered")
	}
	login.URLs = nil
	refused(t, input(t, login), "is missing URL")
	login.URLs = []connecttest.URL{{Href: "https://login.example.com\nINJECTED=x"}}
	refused(t, input(t, login), "Invalid controller field value")
}

func TestConnectionRefusals(t *testing.T) {
	base := func(change func(*item)) Input {
		connection := apiToken(1, "example", "EXAMPLE")
		change(&connection)
		return input(t, connection)
	}
	set := func(label, value string) func(*item) {
		return func(i *item) {
			for index := range i.Fields {
				if i.Fields[index].Label == label {
					i.Fields[index].Value = &value
				}
			}
		}
	}
	drop := func(label string) func(*item) {
		return func(i *item) {
			kept := []field{}
			for _, each := range i.Fields {
				if each.Label != label {
					kept = append(kept, each)
				}
			}
			i.Fields = kept
		}
	}
	add := func(extra ...field) func(*item) {
		return func(i *item) { i.Fields = append(i.Fields, extra...) }
	}
	cases := []struct {
		name string
		in   Input
		want string
	}{
		{"undeclared projection", base(drop("projection")), "declares no projection or env_prefix"},
		{"undeclared prefix", base(drop("env_prefix")), "declares no projection or env_prefix"},
		{"unknown projection", base(set("projection", "nonexistent")), "names an unknown projection"},
		{"invalid prefix", base(set("env_prefix", "EXAMPLE;id")), "invalid env_prefix"},
		{"lowercase prefix", base(set("env_prefix", "example")), "invalid env_prefix"},
		{"prefix of the controller's own settings", base(set("env_prefix", "HQ")), "the controller's own settings use"},
		{"prefix under the launcher's namespace", base(set("env_prefix", "HQ_CONTROLLER")), "the controller's own settings use"},
		{"prefix of the application's settings", base(set("env_prefix", "SEVERINO")), "the controller's own settings use"},
		{"prefix of the framework's settings", base(set("env_prefix", "DJANGO_X")), "the controller's own settings use"},
		{"prefix of 1Password's own variables", base(set("env_prefix", "OP")), "the controller's own settings use"},
		{"invalid ref", base(set("connection_ref", "../example")), "invalid connection_ref"},
		{"ref with a space", base(set("connection_ref", "exam ple")), "invalid connection_ref"},
		{"multi-line credential", base(set("credential", "token\nINJECTED=x")), "Invalid controller field value"},
		{"carriage return", base(set("credential", "token\rINJECTED=x")), "Invalid controller field value"},
		{"NUL", base(set("credential", "token\x00")), "Invalid controller field value"},
		{"multi-line unprojected field", base(add(f("notes", "line one\nline two"))), "Invalid controller field value"},
		{"tab", base(set("website", "https://api.example.com\tinjected")), "control character in URL"},
		{"escape", base(set("credential", "token\x1b[0m")), "control character in API_TOKEN"},
		{"missing required field", base(drop("website")), "exactly one field label=website; found 0"},
		{"empty required field", base(set("website", "")), "is missing URL"},
		{"duplicate projected field", base(add(f("website", "https://other.example.com"))), "exactly one field label=website; found 2"},
		{"duplicate credential id", base(add(b("credential", "another"))), "exactly one field id=credential; found 2"},
		{"duplicate env_prefix metadata", base(add(f("env_prefix", "OTHER"))), "Duplicate connection metadata"},
		{"duplicate projection metadata", base(add(f("projection", "login"))), "Duplicate connection metadata"},
		{"duplicate connection_ref metadata", base(add(f("connection_ref", "other"))), "Duplicate connection metadata"},
		{"duplicate bootstrap metadata", base(add(f("bootstrap", "op://Operator Vault/a"), f("bootstrap", "op://Operator Vault/b"))), "Duplicate connection metadata"},
		{"unresolved reference", base(set("credential", "op://Example Vault/item/credential")), "unresolved reference"},
		{"two items, one ref", input(t, apiToken(1, "example", "EXAMPLE"), apiToken(2, "example", "OTHER")), "More than one 1Password item declares connection_ref=example"},
		{"two refs, one prefix", input(t, apiToken(1, "example", "EXAMPLE"), apiToken(2, "other", "EXAMPLE")), "same env_prefix"},
		{"flattened collision", input(t, apiToken(1, "example", "A"),
			item{ID: id(2), Fields: []field{f("connection_ref", "other"), f("projection", "service_account"), f("env_prefix", "A_API"), b("credential", secret)}},
			item{ID: id(3), Fields: []field{f("connection_ref", "third"), f("projection", "api_token"), f("env_prefix", "A_API_TOKEN_X"),
				b("credential", secret), f("website", "https://api.example.com")}}), ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if c.want == "" {
				// Each connection holds its own settings, so no two can collide.
				if _, err := Project(c.in); err != nil {
					t.Fatal(err)
				}
				return
			}
			refused(t, c.in, c.want)
		})
	}
	// A projection that is no shape of the document cannot be written, even
	// from a registry built past the parser.
	in := input(t, item{ID: id(1), Fields: []field{f("connection_ref", "a"), f("projection", "one"), f("env_prefix", "A"), f("website", "https://api.example.com")}})
	in.Registry = Registry{SchemaVersion: 1, Projections: map[string]map[string]Entry{"one": {
		RefVariable: {Source: SourceRef}, "URL": {Source: SourceField, Label: "website"}}}}
	refused(t, in, "names a projection the controller does not read")
	in.Registry.Projections["api_token"] = in.Registry.Projections["one"]
	in.Registry.Projections["api_token"]["EXAMPLE"] = Entry{Source: SourceConstant, Value: "x"}
	in.Items[0] = item{ID: id(1), Fields: []field{f("connection_ref", "a"), f("projection", "api_token"), f("env_prefix", "A"), f("website", "https://api.example.com")}}.Full()
	refused(t, in, "names a projection the controller does not read")
}

func TestItemsThatAreNotConnectionsAreSkipped(t *testing.T) {
	key := connecttest.Ed25519Key(t)
	values := rendered(t, input(t,
		apiToken(1, "example", "EXAMPLE"),
		item{ID: id(2), Title: "Unrelated", Fields: []field{f("username", "someone")}},
		// An SSH key item spans lines, and nothing renders it as a connection.
		connecttest.KeyItem(id(3), "Unused key", key.PKCS8, key.Public),
		item{ID: id(4), Title: "Empty ref", Fields: []field{f("connection_ref", ""), f("notes", "a\nb")}},
		item{ID: id(5), Title: "No fields"}))
	if len(values) != 6 {
		t.Fatalf("rendered %d variables from one connection", len(values))
	}
}

func TestAVaultWithNoConnectionsIsRefused(t *testing.T) {
	refused(t, input(t, item{ID: id(2), Fields: []field{f("username", "someone")}}), "resolved no connections")
	refused(t, input(t), "resolved no connections")
}

func TestBootstrapReferences(t *testing.T) {
	with := func(reference string) Input {
		return input(t, apiToken(1, "example", "EXAMPLE", f("bootstrap", reference)))
	}
	if rendered(t, with("op://Operator Vault/Example bootstrap"))["EXAMPLE_BOOTSTRAP"] != "op://Operator Vault/Example bootstrap" {
		t.Fatal("a bootstrap reference in another vault was not rendered")
	}
	for reference, want := range map[string]string{
		"op://" + connecttest.VaultName + "/Example bootstrap":                  "keeps its bootstrap credential in the vault the controller reads",
		"op://" + connecttest.VaultID + "/Example bootstrap":                    "keeps its bootstrap credential in the vault the controller reads",
		"op://" + connecttest.VaultName + "/Example bootstrap/field":            "keeps its bootstrap credential in the vault the controller reads",
		"op://" + connecttest.VaultName:                                         "keeps its bootstrap credential in the vault the controller reads",
		"op://" + strings.ToLower(connecttest.VaultName) + "/Example bootstrap": "keeps its bootstrap credential in the vault the controller reads",
		"op://" + strings.ToUpper(connecttest.VaultName) + "/Example bootstrap": "keeps its bootstrap credential in the vault the controller reads",
		"op://" + connecttest.VaultName + " /Example bootstrap":                 "keeps its bootstrap credential in the vault the controller reads",
		"op:// " + connecttest.VaultName + "/Example bootstrap":                 "keeps its bootstrap credential in the vault the controller reads",
		"op://" + strings.ToUpper(connecttest.VaultID) + "/Example bootstrap":   "keeps its bootstrap credential in the vault the controller reads",
		"op://Operator Vault/Example bootstrap/credential":                      "name the item",
		"op://Operator Vault/Example bootstrap/section/credential":              "name the item",
		"not-a-reference":         "invalid bootstrap reference",
		"op://":                   "invalid bootstrap reference",
		"op://Operator Vault":     "invalid bootstrap reference",
		"op://Operator Vault/":    "invalid bootstrap reference",
		"op:///Example bootstrap": "invalid bootstrap reference",
		"https://example.com/op://Operator Vault/Example bootstrap": "invalid bootstrap reference",
		"op://Operator Vault/Example\tbootstrap":                    "invalid bootstrap reference",
	} {
		t.Run(reference, func(t *testing.T) { refused(t, with(reference), want) })
	}
	// The host may name the vault by identifier while the item names it by
	// name: both are the vault the renderer reads.
	byID := with("op://" + connecttest.VaultName + "/Example bootstrap")
	byID.Vault.Configured = connecttest.VaultID
	refused(t, byID, "keeps its bootstrap credential in the vault the controller reads")
}

func TestAServiceAccountTokenIsProjectedFromItsItem(t *testing.T) {
	writer := item{ID: id(1), Fields: []field{
		f("connection_ref", "publisher"), f("projection", "service_account"), f("env_prefix", "ONEPASSWORD"),
		f("provider", "onepassword"), b("credential", secret)}}
	values := rendered(t, input(t, writer))
	if values["PUBLISHER_API_TOKEN"] != secret || values["PUBLISHER_PROVIDER"] != "onepassword" ||
		values["PUBLISHER_STORE_ITEM"] != id(1) {
		t.Fatal("the service account token was not projected from its item")
	}
	writer.Fields = writer.Fields[:4]
	refused(t, input(t, writer), "exactly one field id=credential; found 0")
}

func TestHostileValuesRoundTripLiterally(t *testing.T) {
	value := `$(touch /tmp/executed) ` + "`id`" + ` $HOME ' \"quoted" <>&;| {"json":true} é end`
	connection := apiToken(1, "example", "EXAMPLE")
	connection.Fields[3].Value = &value
	out, err := Project(input(t, connection))
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := out.Document.Encode()
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := connections.Decode(encoded)
	if err != nil {
		t.Fatal(err)
	}
	if entries(t, decoded)["EXAMPLE_API_TOKEN"] != value {
		t.Fatal("a value did not survive the document byte for byte")
	}
}

func TestApplicationEnvironment(t *testing.T) {
	in := input(t, apiToken(1, "example", "EXAMPLE"))
	env := item{ID: id(900), Title: "example env", Fields: []field{
		f("PLAIN", "value"), f("QUOTED", `it's a "value" $HOME `+"`id`"), f("MULTI", "line one\nline two"),
		f("lower_case", "skipped"), f("X", "single letters are not variables"), f("EMPTY", ""),
		{ID: "novalue", Type: "STRING", Label: "NO_VALUE"}, f("notes", "skipped"), f("WITH_1_DIGIT", "kept"),
	}}
	for index := range 12 {
		env.Fields = append(env.Fields, f("FILL_"+string(rune('A'+index)), "v"))
	}
	in.Items[len(in.Items)-1] = env.Full()
	out, err := Project(in)
	if err != nil {
		t.Fatal(err)
	}
	want := "PLAIN='value'\nQUOTED='it'\\''s a \"value\" $HOME `id`'\nMULTI='line one\nline two'\nWITH_1_DIGIT='kept'\n"
	if !strings.HasPrefix(string(out.AppEnv), want) {
		t.Fatalf("rendered:\n%s", out.AppEnv)
	}
	if out.AppVariables != 16 || strings.Contains(string(out.AppEnv), "skipped") || strings.Contains(string(out.AppEnv), "EMPTY") {
		t.Fatalf("%d variables:\n%s", out.AppVariables, out.AppEnv)
	}

	replace := func(fields ...field) Input {
		changed := input(t, apiToken(1, "example", "EXAMPLE"))
		changed.Items[len(changed.Items)-1] = item{ID: id(900), Title: "example env", Fields: fields}.Full()
		return changed
	}
	fifteen := envItem(15).Fields
	refused(t, replace(fifteen[:14]...), "suspiciously small app env (14 vars)")
	refused(t, replace(), "empty render")
	refused(t, replace(f("lower", "only")), "empty render")
	refused(t, replace(append(fifteen, f("REFERENCE", "op://Example Vault/item/field"))...), "unresolved reference")
	// A line of a multi-line value that looks like an empty assignment is
	// inside its quotes: it is the value, and it is rendered as written.
	multi, err := Project(replace(append(fifteen, f("MULTI", "first\nINJECTED=\nlast"))...))
	if err != nil || !strings.Contains(string(multi.AppEnv), "MULTI='first\nINJECTED=\nlast'\n") {
		t.Fatalf("a multi-line value was refused or rewritten: %v", err)
	}
	refused(t, replace(append(fifteen, f("BINARY", "a\x00b"))...), "NUL")
	// Read back as text, a carriage return would not be the byte that was written.
	refused(t, replace(append(fifteen, f("WINDOWS", "line one\r\nline two"))...), "carriage return")
	refused(t, replace(append(fifteen, f("BARE", "a\rb"))...), "carriage return")
	// Django takes the first of two, a shell sourcing the file the last.
	refused(t, replace(append(fifteen, f("EXAMPLE_A", "another"))...), "set twice")
	refused(t, replace(append(fifteen, f("TWICE", "one"), f("TWICE", "one"))...), "set twice")
	// A repeated label that renders nothing is not a variable set twice.
	if _, err := Project(replace(append(fifteen, f("notes", "a"), f("notes", "b"), f("EMPTY", ""), f("EMPTY", ""))...)); err != nil {
		t.Fatalf("repeated labels that are not variables were refused: %v", err)
	}

	missing := input(t, apiToken(1, "example", "EXAMPLE"))
	missing.EnvItem = "another env"
	refused(t, missing, "exactly one item; found 0")
	twice := input(t, apiToken(1, "example", "EXAMPLE"), item{ID: id(901), Title: "example env", Fields: fifteen})
	refused(t, twice, "exactly one item; found 2")
	byID := input(t, apiToken(1, "example", "EXAMPLE"))
	byID.EnvItem = id(900)
	if _, err := Project(byID); err != nil {
		t.Fatalf("the environment item was not found by identifier: %v", err)
	}
}

func sshConnection(n int, ref, prefix, identity string, extra ...field) item {
	return item{ID: id(n), Fields: append([]field{
		f("connection_ref", ref), f("projection", "ssh_transport"), f("env_prefix", prefix),
		f("host", "edge.example.com"), f("port", "2222"), f("user", "deploy"),
		f("host_key", hostKey), f("identity", identity),
	}, extra...)}
}

// The host key connections pin: a fresh one each run, never a literal.
var hostKey = connecttest.HostKey()

func files(t *testing.T, out Output) map[string]File {
	t.Helper()
	found := map[string]File{}
	for _, file := range out.SSH {
		found[file.Name] = file
	}
	return found
}

func TestSSHIdentitiesRenderWithPinnedHosts(t *testing.T) {
	for name, private := range map[string]func(connecttest.Key) string{
		"stored as PKCS#8":  func(k connecttest.Key) string { return k.PKCS8 },
		"stored as OpenSSH": func(k connecttest.Key) string { return k.OpenSSH },
	} {
		t.Run(name, func(t *testing.T) {
			key := connecttest.Ed25519Key(t)
			out, err := Project(input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
				connecttest.KeyItem(id(2), "Edge deploy key", private(key), key.Public+" a comment")))
			if err != nil {
				t.Fatal(err)
			}
			found := files(t, out)
			if len(found) != 3 || out.Identities != 1 || out.SigningKeys != 0 {
				t.Fatalf("files: %d", len(found))
			}
			if string(found[KnownHosts].Data) != "[edge.example.com]:2222 "+hostKey+"\n" || found[KnownHosts].Mode != 0o444 {
				t.Fatalf("known_hosts: %q", found[KnownHosts].Data)
			}
			// What ssh reads: an OpenSSH private key for the item's public key.
			block, _ := pem.Decode(found["edge"].Data)
			if block == nil || block.Type != "OPENSSH PRIVATE KEY" || found["edge"].Mode != 0o400 {
				t.Fatal("the identity is not an OpenSSH private key, mode 0400")
			}
			signer, err := ssh.ParsePrivateKey(found["edge"].Data)
			if err != nil || strings.TrimSpace(string(ssh.MarshalAuthorizedKey(signer.PublicKey()))) != key.Public {
				t.Fatalf("the identity is not the item's key: %v", err)
			}
			if string(found["edge.pub"].Data) != key.Public+"\n" || found["edge.pub"].Mode != 0o444 {
				t.Fatalf("public half: %q", found["edge.pub"].Data)
			}
			// A second render of the same key is the same identity, though its
			// bytes differ; another key is not.
			again, err := Project(input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
				connecttest.KeyItem(id(2), "Edge deploy key", private(key), key.Public)))
			if err != nil {
				t.Fatal(err)
			}
			if !found["edge"].Same(files(t, again)["edge"].Data) {
				t.Fatal("the same identity rendered twice reads as changed")
			}
			other := connecttest.Ed25519Key(t)
			if found["edge"].Same([]byte(other.OpenSSH)) || found["edge"].Same([]byte(key.PKCS8)) || found["edge"].Same(nil) {
				t.Fatal("a different or differently encoded key reads as the installed identity")
			}
		})
	}
}

// ssh looks a host up by bare name on port 22 and as [host]:port otherwise,
// and the controller dials the port as a number.
func TestKnownHostsAreWrittenAsSSHLooksThemUp(t *testing.T) {
	key := connecttest.Ed25519Key(t)
	for port, want := range map[string]string{
		"22":    "edge.example.com " + hostKey + "\n",
		"022":   "edge.example.com " + hostKey + "\n",
		"2222":  "[edge.example.com]:2222 " + hostKey + "\n",
		"02222": "[edge.example.com]:2222 " + hostKey + "\n",
		"65535": "[edge.example.com]:65535 " + hostKey + "\n",
	} {
		connection := sshConnection(1, "edge", "EDGE", "Edge deploy key")
		connection.Fields[4].Value = &port
		out, err := Project(input(t, connection, connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, key.Public)))
		if err != nil {
			t.Fatal(err)
		}
		if got := string(files(t, out)[KnownHosts].Data); got != want {
			t.Errorf("port %s pinned as %q", port, got)
		}
	}
}

// The controller refuses a document over its size bound, so one is never
// rendered: the last good document stays.
func TestAConnectionsDocumentTheControllerWouldRefuseIsNotRendered(t *testing.T) {
	huge := apiToken(1, "example", "EXAMPLE")
	value := strings.Repeat("a", connections.MaxBytes)
	huge.Fields[3].Value = &value
	refused(t, input(t, huge), "the controller would not read")
}

func TestIdentityRefusals(t *testing.T) {
	key, other := connecttest.Ed25519Key(t), connecttest.Ed25519Key(t)
	keyItem := connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, key.Public)
	with := func(change func(*item)) Input {
		connection := sshConnection(1, "edge", "EDGE", "Edge deploy key")
		change(&connection)
		return input(t, connection, keyItem)
	}
	set := func(label, value string) func(*item) {
		return func(i *item) {
			for index := range i.Fields {
				if i.Fields[index].Label == label {
					i.Fields[index].Value = &value
				}
			}
		}
	}
	cases := []struct {
		name string
		in   Input
		want string
	}{
		{"halves differ", input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
			connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, other.Public)), "do not match"},
		{"identity is a reference", with(set("identity", "op://Elsewhere/key")), "unresolved reference"},
		{"identity is an op: name", with(set("identity", "op:Elsewhere")), "names an invalid identity item"},
		{"identity holds a slash", with(set("identity", "a/b")), "names an invalid identity item"},
		{"identity item absent", with(set("identity", "No such key")), "the vault holds 0 of"},
		{"identity item ambiguous", input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"), keyItem,
			connecttest.KeyItem(id(3), "Edge deploy key", other.PKCS8, other.Public)), "the vault holds 2 of"},
		{"private key unreadable", input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
			connecttest.KeyItem(id(2), "Edge deploy key", "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", key.Public)), "cannot be read"},
		{"public key unreadable", input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
			connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, "ssh-ed25519 not-base64")), "cannot be read"},
		{"no private key field", input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
			item{ID: id(2), Title: "Edge deploy key", Fields: []field{f("public key", key.Public)}}), "no single private key"},
		{"two private key fields", input(t, sshConnection(1, "edge", "EDGE", "Edge deploy key"),
			item{ID: id(2), Title: "Edge deploy key", Fields: []field{
				f("private key", key.PKCS8), f("private key", other.PKCS8), f("public key", key.Public)}}), "no single private key"},
		{"tab in host", with(set("host", "edge.example.com\tinjected")), "control character in HOST"},
		{"host with option", with(set("host", "-oProxyCommand=x")), "invalid host"},
		{"host with user", with(set("host", "root@edge.example.com")), "invalid host"},
		{"host starting with a dash", with(set("host", "-edge.example.com")), "invalid host"},
		{"user as an option", with(set("user", "-oProxyCommand")), "invalid user"},
		{"user with a host", with(set("user", "root@other")), "invalid user"},
		{"user with a space", with(set("user", "deploy user")), "invalid user"},
		{"port not a number", with(set("port", "22x")), "invalid port"},
		{"port zero", with(set("port", "0")), "invalid port"},
		{"port too large", with(set("port", "65536")), "invalid port"},
		{"port signed", with(set("port", "+22")), "invalid port"},
		{"rsa host key", with(set("host_key", "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQC7")), "must pin an ssh-ed25519 host key"},
		{"malformed host key", with(set("host_key", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample")), "must pin an ssh-ed25519 host key"},
		{"host key with options", with(set("host_key", `command="x" `+hostKey)), "must pin an ssh-ed25519 host key"},
		{"ref named known_hosts", input(t, sshConnection(1, "known_hosts", "EDGE", "Edge deploy key"), keyItem), "collide"},
		{"ref ending .pub", input(t, sshConnection(1, "edge.pub", "EDGE", "Edge deploy key"), keyItem), "collide"},
		{"ref ending .key", input(t, sshConnection(1, "edge.key", "EDGE", "Edge deploy key"), keyItem), "collide"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			refused(t, c.in, c.want)
			if _, err := Project(c.in); strings.Contains(err.Error(), "PRIVATE KEY") {
				t.Fatal("the refusal quotes key material")
			}
		})
	}
}

func signingConnection(n int, ref, key string, extra ...field) item {
	return item{ID: id(n), Fields: append([]field{
		f("connection_ref", ref), f("projection", "github_app"), f("env_prefix", "GITHUB"),
		f("app_id", "12345"), f("signing_key", key),
	}, extra...)}
}

func TestASigningKeyRendersAsPKCS8BesideTheIdentities(t *testing.T) {
	key := connecttest.RSAKey(t)
	out, err := Project(input(t, signingConnection(1, "github", "Example app key"),
		connecttest.KeyItem(id(2), "Example app key", key.PKCS8, key.Public)))
	if err != nil {
		t.Fatal(err)
	}
	found := files(t, out)
	if len(found) != 3 || out.SigningKeys != 1 || len(found[KnownHosts].Data) != 0 {
		t.Fatalf("files: %d", len(found))
	}
	// Byte for byte what the item stores, which is what openssl reads.
	if string(found["github.key"].Data) != key.PKCS8 || found["github.key"].Mode != 0o400 {
		t.Fatal("the signing key is not the item's PKCS#8 key, mode 0400")
	}
	block, _ := pem.Decode(found["github.key"].Data)
	if _, err := x509.ParsePKCS8PrivateKey(block.Bytes); err != nil {
		t.Fatal(err)
	}
	if string(found["github.key.pub"].Data) != key.Public+"\n" || found["github.key.pub"].Mode != 0o444 {
		t.Fatal("public half")
	}
	// Deterministic: an unchanged signing key is byte-identical on every render.
	again, _ := Project(input(t, signingConnection(1, "github", "Example app key"),
		connecttest.KeyItem(id(2), "Example app key", key.OpenSSH, key.Public)))
	if sha256.Sum256(files(t, again)["github.key"].Data) != sha256.Sum256(found["github.key"].Data) {
		t.Fatal("the same signing key rendered differently")
	}
}

func TestSigningKeyRefusals(t *testing.T) {
	key, other := connecttest.RSAKey(t), connecttest.Ed25519Key(t)
	keyItem := connecttest.KeyItem(id(2), "Example app key", key.PKCS8, key.Public)
	refused(t, input(t, signingConnection(1, "github", "Example app key"),
		connecttest.KeyItem(id(2), "Example app key", key.PKCS8, other.Public)), "do not match")
	refused(t, input(t, signingConnection(1, "github", "op://Elsewhere/key"), keyItem), "unresolved reference")
	refused(t, input(t, signingConnection(1, "github", "op:Elsewhere"), keyItem), "names an invalid signing key item")
	refused(t, input(t, signingConnection(1, "github", "a/b"), keyItem), "names an invalid signing key item")
	refused(t, input(t, signingConnection(1, "github.key", "Example app key"), keyItem), "invalid name")
	refused(t, input(t, signingConnection(1, "known_hosts", "Example app key"), keyItem), "invalid name")
	refused(t, input(t, signingConnection(1, "github", "No such key"), keyItem), "the vault holds 0 of")
}

func TestRegistryRefusals(t *testing.T) {
	good := `{"schema_version":1,"projections":{"p":{"CONNECTION_REF":{"source":"connection_ref"},"URL":{"source":"field","label":"website"}}}}`
	if _, err := parse([]byte(good)); err != nil {
		t.Fatal(err)
	}
	entry := func(body string) string {
		return strings.Replace(good, `{"source":"field","label":"website"}`, body, 1)
	}
	for name, text := range map[string]string{
		"not json":               `projections`,
		"wrong schema version":   strings.Replace(good, `"schema_version":1`, `"schema_version":2`, 1),
		"no projections":         `{"schema_version":1,"projections":{}}`,
		"projections not object": `{"schema_version":1,"projections":[]}`,
		// The vault is the inventory: a registry that lists connections is refused.
		"a connections key":         strings.Replace(good, `{"schema_version"`, `{"connections":{},"schema_version"`, 1),
		"unknown entry key":         entry(`{"source":"field","label":"website","optinal":true}`),
		"unknown source":            entry(`{"source":"environment","label":"website"}`),
		"no source":                 entry(`{"label":"website"}`),
		"field with id and label":   entry(`{"source":"field","id":"a","label":"b"}`),
		"field with neither":        entry(`{"source":"field"}`),
		"constant with no value":    entry(`{"source":"constant"}`),
		"constant with a selector":  entry(`{"source":"constant","value":"x","label":"y"}`),
		"url with no index":         entry(`{"source":"url"}`),
		"url with negative index":   entry(`{"source":"url","index":-1}`),
		"lowercase variable":        strings.Replace(good, `"URL"`, `"url"`, 1),
		"renderer's own variable":   strings.Replace(good, `"URL"`, `"STORE_ITEM"`, 1),
		"bootstrap variable":        strings.Replace(good, `"URL"`, `"BOOTSTRAP"`, 1),
		"no connection ref":         `{"schema_version":1,"projections":{"p":{"URL":{"source":"field","label":"website"}}}}`,
		"connection ref from field": strings.Replace(good, `{"source":"connection_ref"}`, `{"source":"field","label":"connection_ref"}`, 1),
		"repeated projection":       strings.Replace(good, `"projections":{`, `"projections":{"p":{"CONNECTION_REF":{"source":"connection_ref"}},`, 1),
		"trailing data":             good + `{}`,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parse([]byte(text)); !errors.Is(err, ErrRegistry) {
				t.Fatalf("accepted: %v", err)
			}
		})
	}
	// Well formed, and still not the shapes a connection arrives in.
	if _, err := ParseRegistry([]byte(good)); !errors.Is(err, ErrRegistry) {
		t.Fatalf("a registry of other shapes was accepted: %v", err)
	}
}

// The registry states where each setting comes from; which settings a shape
// has is the generated type's to say. A registry that disagrees is refused
// whole, so a renderer never writes a setting the controller does not read.
func TestARegistryThatIsNotTheDocumentsShapesIsRefused(t *testing.T) {
	data, err := os.ReadFile(shipped)
	if err != nil {
		t.Fatal(err)
	}
	change := func(edit func(projections map[string]map[string]Entry)) []byte {
		t.Helper()
		parsed, err := parse(data)
		if err != nil {
			t.Fatal(err)
		}
		edit(parsed.Projections)
		encoded, err := json.Marshal(parsed)
		if err != nil {
			t.Fatal(err)
		}
		return encoded
	}
	if _, err := ParseRegistry(change(func(map[string]map[string]Entry) {})); err != nil {
		t.Fatalf("the shipped registry does not survive a round trip: %v", err)
	}
	index := 0
	for name, edit := range map[string]func(map[string]map[string]Entry){
		"a shape with no projection": func(p map[string]map[string]Entry) { delete(p, "login") },
		"a projection that is no shape": func(p map[string]map[string]Entry) {
			p["example_shape"] = map[string]Entry{RefVariable: {Source: SourceRef}}
		},
		"a renamed projection": func(p map[string]map[string]Entry) { p["sign_in"] = p["login"]; delete(p, "login") },
		"a setting left out":   func(p map[string]map[string]Entry) { delete(p["login"], "USERNAME") },
		"a setting added": func(p map[string]map[string]Entry) {
			p["service_account"]["URL"] = Entry{Source: SourceURL, Index: &index}
		},
		"a setting renamed": func(p map[string]map[string]Entry) {
			p["login"]["USER"] = p["login"]["USERNAME"]
			delete(p["login"], "USERNAME")
		},
		"a required setting made optional": func(p map[string]map[string]Entry) {
			entry := p["login"]["PASSWORD"]
			entry.Optional = true
			p["login"]["PASSWORD"] = entry
		},
		"an optional setting made required": func(p map[string]map[string]Entry) {
			entry := p["ssh_transport"]["ROLE"]
			entry.Optional = false
			p["ssh_transport"]["ROLE"] = entry
		},
		"a default for a required field": func(p map[string]map[string]Entry) {
			entry := p["login"]["USERNAME"]
			entry.Default = "reader"
			p["login"]["USERNAME"] = entry
		},
		"a default for a constant": func(p map[string]map[string]Entry) {
			entry := p["github_app"][ProviderVariable]
			entry.Default = "other"
			p["github_app"][ProviderVariable] = entry
		},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseRegistry(change(edit)); !errors.Is(err, ErrRegistry) {
				t.Fatalf("accepted: %v", err)
			}
		})
	}
}

// The registry the repository ships is emitted from the connection shapes
// HQ's registry declares, as the document's types are: one declaration.
func TestTheShippedRegistryParses(t *testing.T) {
	data, err := os.ReadFile(shipped)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseRegistry(data)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"acme", "api_token", "github_app", "login", "oauth_client", "service_account", "ssh_transport"} {
		if _, ok := parsed.Projections[name]; !ok {
			t.Errorf("the registry declares no %s projection", name)
		}
	}
	if token := parsed.Projections["service_account"]["API_TOKEN"]; token.Source != SourceField || token.ID != "credential" {
		t.Fatal("the service account token is not projected from the item's credential field")
	}
	if bytes.Contains(data, []byte(`"connections"`)) {
		t.Fatal("the registry names connections; the vault is the inventory")
	}
}

func FuzzProject(f *testing.F) {
	f.Add("example", "api_token", "EXAMPLE", "token", "https://api.example.com", "op://Operator Vault/item", "edge.example.com", "22")
	f.Add("known_hosts", "ssh_transport", "EDGE", "tok\nen", "x", "op://"+connecttest.VaultName+"/item", "-oProxyCommand=x", "0")
	f.Add("a.key", "github_app", "A_B", "\x00", "", "not-a-reference", "host\tname", "65536")
	f.Add("", "login", "example", "op://x/y/z", "\r", "", "", "")
	key := connecttest.Ed25519Key(f)
	parsed := registry(f)
	f.Fuzz(func(t *testing.T, ref, projection, prefix, credential, website, bootstrap, host, port string) {
		connection := connecttest.Item{ID: id(1), Fields: []field{
			connecttest.F("connection_ref", ref), connecttest.F("projection", projection), connecttest.F("env_prefix", prefix),
			b("credential", credential), b("password", credential), connecttest.F("website", website),
			connecttest.F("bootstrap", bootstrap), connecttest.F("host", host), connecttest.F("port", port),
			connecttest.F("user", "deploy"), connecttest.F("host_key", hostKey), connecttest.F("identity", "Edge deploy key"),
			connecttest.F("app_id", "1"), connecttest.F("signing_key", "Edge deploy key"),
		}, URLs: []connecttest.URL{{Href: website}}}
		in := Input{Registry: parsed, EnvItem: "example env", MinAppVariables: 15,
			Vault: Vault{Configured: connecttest.VaultName, ID: connecttest.VaultID, Name: connecttest.VaultName},
			Items: []connectapi.FullItem{connection.Full(), envItem(15).Full(),
				connecttest.KeyItem(id(2), "Edge deploy key", key.PKCS8, key.Public).Full()}}
		out, err := Project(in)
		if err != nil {
			if !errors.Is(err, ErrContent) {
				t.Fatalf("an untyped refusal: %v", err)
			}
			if len(credential) > 8 && strings.Contains(err.Error(), credential) {
				t.Fatalf("the refusal carries the credential: %v", err)
			}
			return
		}
		// Whatever renders, the controller must be able to read back exactly.
		encoded, err := out.Document.Encode()
		if err != nil {
			t.Fatalf("rendered a document that does not encode: %v", err)
		}
		decoded, err := connections.Decode(encoded)
		if err != nil {
			t.Fatalf("rendered a document the controller refuses: %v", err)
		}
		for name, value := range entries(t, decoded) {
			if strings.ContainsAny(value, "\x00\r\n\t") || value == "" {
				t.Fatalf("%s rendered a control character or nothing", name)
			}
			if strings.Contains(value, "op://") && !strings.HasSuffix(name, "_"+Bootstrap) {
				t.Fatalf("%s rendered an unresolved reference", name)
			}
		}
		for _, rendered := range decoded.Connections {
			reference := rendered.Store.Bootstrap
			if reference == "" {
				continue
			}
			rest := strings.TrimPrefix(reference, "op://")
			if strings.HasPrefix(rest, connecttest.VaultName+"/") || strings.HasPrefix(rest, connecttest.VaultID+"/") || strings.Count(rest, "/") != 1 {
				t.Fatalf("rendered a bootstrap in the vault being read, or a field reference")
			}
		}
		for _, file := range out.SSH {
			if strings.Contains(file.Name, "/") || strings.HasPrefix(file.Name, ".") {
				t.Fatalf("rendered a file name that leaves the directory: %q", file.Name)
			}
		}
	})
}
