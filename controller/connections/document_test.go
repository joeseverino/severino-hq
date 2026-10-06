package connections

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const secret = "sentinel-document-secret"

var kept = Store{Vault: "example-vault", Item: "example-item"}

func valid() Document {
	return Document{SchemaVersion: SchemaVersion, Connections: []Connection{
		{Ref: "example-b", Provider: "example", Store: kept, ServiceAccount: &ServiceAccount{APIToken: secret}},
		{Ref: "example-a", Provider: "example", Manages: true, Store: kept, Login: &Login{URL: "https://a.example.com", Username: "reader", Password: secret}},
	}}
}

func TestEncodeIsDeterministicAndRoundTrips(t *testing.T) {
	first, err := valid().Encode()
	if err != nil {
		t.Fatal(err)
	}
	reordered := valid()
	reordered.Connections[0], reordered.Connections[1] = reordered.Connections[1], reordered.Connections[0]
	second, err := reordered.Encode()
	if err != nil || string(first) != string(second) {
		t.Fatalf("encoding depends on order: %v", err)
	}
	decoded, err := Decode(first)
	if err != nil {
		t.Fatal(err)
	}
	a, b := decoded.Connections[0], decoded.Connections[1]
	if a.Ref != "example-a" || !a.Manages || a.Login == nil || a.Login.Password != secret || a.Shape() != "login" {
		t.Fatalf("the first connection did not round trip: %s", a)
	}
	if b.Ref != "example-b" || b.Manages || b.ServiceAccount == nil || b.ServiceAccount.APIToken != secret || b.Login != nil {
		t.Fatalf("the second connection did not round trip: %s", b)
	}
}

func TestDecodeRefusals(t *testing.T) {
	good := `{"schema_version":2,"connections":[{"ref":"a","provider":"example","manages":false,"store":{"vault":"v","item":"i"},"service_account":{"api_token":"` + secret + `"}}]}`
	if _, err := Decode([]byte(good)); err != nil {
		t.Fatal(err)
	}
	second := `,{"ref":"a","provider":"example","manages":false,"store":{"vault":"v","item":"i"},"service_account":{"api_token":"x"}}]}`
	cases := map[string]string{
		"an older schema version": strings.Replace(good, `"schema_version":2`, `"schema_version":1`, 1),
		"missing schema version":  strings.Replace(good, `"schema_version":2,`, ``, 1),
		"unknown top field":       strings.Replace(good, `{"schema_version"`, `{"extra":true,"schema_version"`, 1),
		"unknown connection key":  strings.Replace(good, `"ref":"a"`, `"ref":"a","note":"x"`, 1),
		"the older format":        `{"schema_version":2,"connections":[{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a"}}]}`,
		"trailing value":          good + `{}`,
		"trailing garbage":        good + `x`,
		"not json":                `SECRET=` + secret,
		"empty":                   ``,
		"no connections":          `{"schema_version":2,"connections":[]}`,
		"null connections":        `{"schema_version":2,"connections":null}`,
		"invalid ref":             strings.Replace(good, `"ref":"a"`, `"ref":"../a"`, 1),
		"no provider":             strings.Replace(good, `"provider":"example"`, `"provider":""`, 1),
		"multi-line provider":     strings.Replace(good, `"provider":"example"`, `"provider":"a\nb"`, 1),
		"no shape":                strings.Replace(good, `,"service_account":{"api_token":"`+secret+`"}`, ``, 1),
		"two shapes":              strings.Replace(good, `"service_account"`, `"oauth_client":{"client_id":"i","client_secret":"s"},"service_account"`, 1),
		"an unknown shape":        strings.Replace(good, `"service_account"`, `"example_shape"`, 1),
		"an undeclared setting":   strings.Replace(good, `"api_token":"`, `"url":"https://a.example.com","api_token":"`, 1),
		"setting name case":       strings.Replace(good, `"api_token"`, `"API_TOKEN"`, 1),
		"multi-line value":        strings.Replace(good, secret, secret+`\nINJECTED=x`, 1),
		"nul in value":            strings.Replace(good, secret, secret+`\u0000`, 1),
		"empty required value":    strings.Replace(good, secret, ``, 1),
		"missing required value":  strings.Replace(good, `"api_token":"`+secret+`"`, ``, 1),
		"no store":                strings.Replace(good, `"store":{"vault":"v","item":"i"},`, ``, 1),
		"an empty store item":     strings.Replace(good, `"item":"i"`, `"item":""`, 1),
		"a multi-line bootstrap":  strings.Replace(good, `"item":"i"`, `"item":"i","bootstrap":"op://a/b\nc"`, 1),
		"duplicate ref":           strings.Replace(good, `]}`, second, 1),
		"repeated setting":        strings.Replace(good, `"api_token":"`, `"api_token":"first","api_token":"`, 1),
		"repeated field":          strings.Replace(good, `"provider":"example"`, `"provider":"other","provider":"example"`, 1),
		"field case":              strings.Replace(good, `"provider"`, `"Provider"`, 1),
		"value wrong type":        strings.Replace(good, `"`+secret+`"`, `7`, 1),
		"manages wrong type":      strings.Replace(good, `"manages":false`, `"manages":"yes"`, 1),
	}
	for name, input := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := Decode([]byte(input))
			if !errors.Is(err, ErrInvalid) {
				t.Fatalf("accepted: %v", err)
			}
			if strings.Contains(err.Error(), secret) {
				t.Fatalf("the error carries a value: %v", err)
			}
		})
	}
	if _, err := Decode(make([]byte, MaxBytes+1)); !errors.Is(err, ErrInvalid) {
		t.Fatal("an oversized document was accepted")
	}
}

// An optional setting may be absent; a required one may not.
func TestAnOptionalSettingMayBeAbsent(t *testing.T) {
	transport := Connection{Ref: "edge", Provider: "ssh", Store: kept, SSHTransport: &SSHTransport{
		Host: "edge.example.com", HostKey: "ssh-ed25519 example", Identity: "example identity", Port: "22", User: "deploy"}}
	document := Document{SchemaVersion: SchemaVersion, Connections: []Connection{transport}}
	encoded, err := document.Encode()
	if err != nil {
		t.Fatalf("a transport with no role was refused: %v", err)
	}
	if bytes.Contains(encoded, []byte(`"role"`)) {
		t.Fatal("an absent optional setting was written")
	}
	transport.SSHTransport = &SSHTransport{Host: "edge.example.com", Port: "22", User: "deploy"}
	document.Connections[0] = transport
	if _, err := document.Encode(); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a transport with no host key was encoded: %v", err)
	}
}

func TestEncodeRefusesADocumentDecodeWouldCallTooLarge(t *testing.T) {
	large := valid()
	large.Connections[0].ServiceAccount = &ServiceAccount{APIToken: strings.Repeat("a", MaxBytes-600)}
	encoded, err := large.Encode()
	if err != nil {
		t.Fatalf("a document under the bound was refused: %v", err)
	}
	if _, err := Decode(encoded); err != nil {
		t.Fatalf("what encodes does not decode: %v", err)
	}
	large.Connections[0].ServiceAccount = &ServiceAccount{APIToken: strings.Repeat("a", MaxBytes)}
	if _, err := large.Encode(); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a document the reader refuses was encoded: %v", err)
	}
}

func TestEncodeRefusesWhatDecodeWould(t *testing.T) {
	broken := valid()
	broken.Connections[0].ServiceAccount = &ServiceAccount{APIToken: "a\nb"}
	if _, err := broken.Encode(); !errors.Is(err, ErrInvalid) {
		t.Fatalf("encoded a multi-line value: %v", err)
	}
}

// Every shape is a member of the generated Connection, and every setting
// carries the name the registry gives it.
func TestShapesAreReadFromTheGeneratedType(t *testing.T) {
	found := Shapes()
	if len(found) == 0 {
		t.Fatal("no shapes")
	}
	names := map[string]bool{}
	for _, shape := range found {
		if shape.Name == "" || names[shape.Name] || len(shape.Settings) == 0 {
			t.Fatalf("shape %q is not declared whole", shape.Name)
		}
		names[shape.Name] = true
		for _, setting := range shape.Settings {
			if !ValidName(setting.Name) {
				t.Errorf("%s has a setting with no registry name", shape.Name)
			}
		}
	}
	for _, name := range []string{"login", "api_token", "ssh_transport"} {
		if !names[name] {
			t.Errorf("no %s shape", name)
		}
	}
}

// A setting is a string and nothing else: no connection type holds a map or
// an untyped value a setting could be looked up in by name.
func TestNoConnectionTypeHoldsSettingsByName(t *testing.T) {
	var walk func(reflect.Type, string)
	walk = func(kind reflect.Type, path string) {
		switch kind.Kind() {
		case reflect.Map, reflect.Interface:
			t.Errorf("%s is a %s: a setting could be read from it by name", path, kind.Kind())
		case reflect.Pointer, reflect.Slice:
			walk(kind.Elem(), path)
		case reflect.Struct:
			for index := range kind.NumField() {
				walk(kind.Field(index).Type, path+"."+kind.Field(index).Name)
			}
		}
	}
	walk(reflect.TypeFor[Document](), "Document")
}

func TestWithSettingsNamesOnlyWhatTheShapeDeclares(t *testing.T) {
	base := Connection{Ref: "example", Provider: "example", Store: kept}
	typed, err := base.WithSettings("login", map[string]string{"URL": "https://a.example.com", "USERNAME": "reader", "PASSWORD": secret})
	if err != nil || typed.Login == nil || typed.Login.Username != "reader" || typed.Login.Password != secret || typed.Shape() != "login" {
		t.Fatalf("a login was not typed: %v", err)
	}
	if base.Login != nil {
		t.Fatal("the connection it was built from changed")
	}
	if _, err := base.WithSettings("login", map[string]string{"URL": "https://a.example.com", "API_TOKEN": secret}); !errors.Is(err, ErrInvalid) || strings.Contains(err.Error(), secret) {
		t.Fatalf("a setting the shape does not declare was accepted: %v", err)
	}
	if _, err := base.WithSettings("example_shape", nil); !errors.Is(err, ErrInvalid) {
		t.Fatalf("an unknown shape was accepted: %v", err)
	}
}

func TestTrimmedAndComplete(t *testing.T) {
	spaced := Connection{Ref: "example", Provider: " example ", Store: kept, Login: &Login{URL: " https://a.example.com ", Username: "reader", Password: " "}}
	trimmed := spaced.Trimmed()
	if trimmed.Provider != "example" || trimmed.Login.URL != "https://a.example.com" || trimmed.Login.Password != "" {
		t.Fatalf("not trimmed: %q %q", trimmed.Provider, trimmed.Login.URL)
	}
	if spaced.Login.URL != " https://a.example.com " {
		t.Fatal("trimming changed the connection it was given")
	}
	if trimmed.Complete() {
		t.Fatal("a login with a blank password is complete")
	}
	trimmed.Login.Password = secret
	if !trimmed.Complete() {
		t.Fatal("a whole login is not complete")
	}
	if (Connection{Ref: "example"}).Complete() {
		t.Fatal("a connection with no shape is complete")
	}
}

// A connection that reaches a log or an error by accident names itself and
// carries none of its settings, whatever the verb.
func TestAConnectionNeverPrintsItsSettings(t *testing.T) {
	for _, shape := range Shapes() {
		settings := map[string]string{}
		for index, setting := range shape.Settings {
			settings[setting.Name] = fmt.Sprintf("%s-%d", secret, index)
		}
		connection, err := (Connection{Ref: "example", Provider: "example", Store: kept}).WithSettings(shape.Name, settings)
		if err != nil {
			t.Fatal(err)
		}
		document := Document{SchemaVersion: SchemaVersion, Connections: []Connection{connection}}
		var logged bytes.Buffer
		slog.New(slog.NewJSONHandler(&logged, nil)).Info("example", slog.Any("connection", connection), slog.Any("pointer", &connection))
		for _, printed := range []string{
			fmt.Sprint(connection), fmt.Sprintf("%v", connection), fmt.Sprintf("%+v", connection), fmt.Sprintf("%#v", connection),
			fmt.Sprintf("%s", connection), fmt.Sprintf("%+v", &connection), fmt.Sprintf("%+v", document), fmt.Sprintf("%#v", document.Connections),
			fmt.Errorf("refused: %v", connection).Error(), logged.String(),
		} {
			if strings.Contains(printed, secret) {
				t.Fatalf("a %s connection printed a setting: %s", shape.Name, printed)
			}
			if !strings.Contains(printed, "example") {
				t.Fatalf("a %s connection did not name itself: %s", shape.Name, printed)
			}
		}
	}
}

// The document on disk is the schema HQ's registry emits: every member the
// generated types write is one the schema declares, under the same name.
func TestTheDocumentIsTheEmittedSchema(t *testing.T) {
	data, err := os.ReadFile("../api/hq-connections.openapi.json")
	if err != nil {
		t.Fatal(err)
	}
	var schema struct {
		Components struct {
			Schemas map[string]struct {
				Properties map[string]json.RawMessage `json:"properties"`
				Required   []string                   `json:"required"`
			} `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatal(err)
	}
	declared := schema.Components.Schemas["Connection"].Properties
	for _, shape := range Shapes() {
		if _, ok := declared[shape.Name]; !ok {
			t.Errorf("the schema declares no %s member", shape.Name)
		}
	}
	if len(declared) != len(Shapes())+4 {
		t.Errorf("the schema declares %d members of a connection; the type has %d", len(declared), len(Shapes())+4)
	}
}

func TestReadFileRefusesUnsafeFiles(t *testing.T) {
	dir := t.TempDir()
	data, err := valid().Encode()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "connections.json")
	if err := os.WriteFile(path, data, 0o400); err != nil {
		t.Fatal(err)
	}
	uid := os.Getuid()
	if _, err := ReadFile(path, uid); err != nil {
		t.Fatalf("a private file was refused: %v", err)
	}
	if _, err := ReadFile(path, uid+1); !errors.Is(err, ErrInvalid) {
		t.Fatal("another account's file was accepted")
	}
	for _, mode := range []os.FileMode{0o440, 0o404, 0o420, 0o644} {
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
		if _, err := ReadFile(path, uid); !errors.Is(err, ErrInvalid) {
			t.Fatalf("mode %o was accepted", mode)
		}
	}
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link.json")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadFile(link, uid); !errors.Is(err, ErrInvalid) {
		t.Fatal("a symlink was followed")
	}
	hard := filepath.Join(dir, "hard.json")
	if err := os.Link(path, hard); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadFile(path, uid); !errors.Is(err, ErrInvalid) {
		t.Fatal("a hard-linked file was accepted")
	}
	if _, err := ReadFile(dir, uid); !errors.Is(err, ErrInvalid) {
		t.Fatal("a directory was accepted")
	}
	if _, err := ReadFile(filepath.Join(dir, "absent.json"), uid); !errors.Is(err, ErrInvalid) {
		t.Fatal("a missing file was accepted")
	}
}

func FuzzDecode(f *testing.F) {
	good, _ := valid().Encode()
	f.Add(good)
	f.Add([]byte(`{"schema_version":2,"connections":[{"ref":"a","provider":"p","manages":true,"store":{"vault":"v","item":"i"},"oauth_client":{"client_id":"i","client_secret":"s"}}]}`))
	f.Add([]byte(`{"schema_version":2,"connections":[{"ref":"a","provider":"p","manages":true,"store":{"vault":"v","item":"i"},"oauth_client":{"client_id":"i","client_secret":"s"}}]}{}`))
	f.Add([]byte(`{"schema_version":1,"connections":[{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a"}}]}`))
	f.Add([]byte(`[]`))
	f.Fuzz(func(t *testing.T, data []byte) {
		document, err := Decode(data)
		if err != nil {
			if !errors.Is(err, ErrInvalid) {
				t.Fatalf("an untyped refusal: %v", err)
			}
			return
		}
		// What decodes must re-encode, decode to the same connections, and
		// hold every invariant a reader relies on.
		encoded, err := document.Encode()
		if err != nil {
			t.Fatalf("decoded but not encodable: %v", err)
		}
		again, err := Decode(encoded)
		if err != nil {
			t.Fatalf("encoded but not decodable: %v", err)
		}
		reencoded, err := again.Encode()
		if err != nil || !bytes.Equal(encoded, reencoded) {
			t.Fatal("round trip changed the connections")
		}
		for _, connection := range again.Connections {
			if !ValidRef(connection.Ref) || connection.Shape() == "" || connection.Provider == "" {
				t.Fatal("round trip admitted a connection a reader cannot use")
			}
		}
	})
}
