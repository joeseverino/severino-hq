package connections

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const secret = "sentinel-document-secret"

func valid() Document {
	return Document{SchemaVersion: SchemaVersion, Connections: []Connection{
		{Ref: "example-b", Prefix: "B", Values: map[string]string{RefName: "example-b", "API_TOKEN": secret}},
		{Ref: "example-a", Prefix: "A", Values: map[string]string{RefName: "example-a", "URL": "https://a.example.com"}},
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
	entries := decoded.Entries()
	if entries["B_API_TOKEN"] != secret || entries["A_CONNECTION_REF"] != "example-a" || len(entries) != 4 {
		t.Fatalf("entries: %d", len(entries))
	}
}

func TestDecodeRefusals(t *testing.T) {
	good := `{"schema_version":1,"connections":[{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a","TOKEN":"` + secret + `"}}]}`
	if _, err := Decode([]byte(good)); err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"wrong schema version":   strings.Replace(good, `"schema_version":1`, `"schema_version":2`, 1),
		"missing schema version": strings.Replace(good, `"schema_version":1,`, ``, 1),
		"unknown top field":      strings.Replace(good, `{"schema_version"`, `{"extra":true,"schema_version"`, 1),
		"unknown connection key": strings.Replace(good, `"ref":"a"`, `"ref":"a","note":"x"`, 1),
		"trailing value":         good + `{}`,
		"trailing garbage":       good + `x`,
		"not json":               `SECRET=` + secret,
		"empty":                  ``,
		"no connections":         `{"schema_version":1,"connections":[]}`,
		"null connections":       `{"schema_version":1,"connections":null}`,
		"invalid ref":            strings.Replace(strings.Replace(good, `"ref":"a"`, `"ref":"../a"`, 1), `"CONNECTION_REF":"a"`, `"CONNECTION_REF":"../a"`, 1),
		"invalid prefix":         strings.Replace(good, `"prefix":"A"`, `"prefix":"A;id"`, 1),
		"lowercase name":         strings.Replace(good, `"TOKEN"`, `"token"`, 1),
		"ref not carried":        strings.Replace(good, `"CONNECTION_REF":"a"`, `"CONNECTION_REF":"b"`, 1),
		"multi-line value":       strings.Replace(good, secret, secret+`\nINJECTED=x`, 1),
		"nul in value":           strings.Replace(good, secret, secret+`\u0000`, 1),
		"empty value":            strings.Replace(good, secret, ``, 1),
		"duplicate ref":          strings.Replace(good, `]}`, `,{"ref":"a","prefix":"B","values":{"CONNECTION_REF":"a"}}]}`, 1),
		"duplicate prefix":       strings.Replace(good, `]}`, `,{"ref":"b","prefix":"A","values":{"CONNECTION_REF":"b"}}]}`, 1),
		"flattened collision": `{"schema_version":1,"connections":[` +
			`{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a","B_CONNECTION_REF":"x"}},` +
			`{"ref":"b","prefix":"A_B","values":{"CONNECTION_REF":"b"}}]}`,
		"repeated value name": strings.Replace(good, `"TOKEN":"`, `"TOKEN":"first","TOKEN":"`, 1),
		"repeated field":      strings.Replace(good, `"prefix":"A"`, `"prefix":"Z","prefix":"A"`, 1),
		"field case":          strings.Replace(good, `"prefix"`, `"Prefix"`, 1),
		"values wrong type":   strings.Replace(good, `"`+secret+`"`, `7`, 1),
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

func TestEncodeRefusesWhatDecodeWould(t *testing.T) {
	broken := valid()
	broken.Connections[0].Values["API_TOKEN"] = "a\nb"
	if _, err := broken.Encode(); !errors.Is(err, ErrInvalid) {
		t.Fatalf("encoded a multi-line value: %v", err)
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
	f.Add([]byte(`{"schema_version":1,"connections":[{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a"}}]}`))
	f.Add([]byte(`{"schema_version":1,"connections":[{"ref":"a","prefix":"A","values":{"CONNECTION_REF":"a"}}]}{}`))
	f.Add([]byte(`[]`))
	f.Fuzz(func(t *testing.T, data []byte) {
		document, err := Decode(data)
		if err != nil {
			if !errors.Is(err, ErrInvalid) {
				t.Fatalf("an untyped refusal: %v", err)
			}
			return
		}
		// What decodes must re-encode, decode to the same settings, and hold
		// every invariant a reader relies on.
		encoded, err := document.Encode()
		if err != nil {
			t.Fatalf("decoded but not encodable: %v", err)
		}
		again, err := Decode(encoded)
		if err != nil {
			t.Fatalf("encoded but not decodable: %v", err)
		}
		first, second := document.Entries(), again.Entries()
		if len(first) != len(second) {
			t.Fatal("round trip changed the settings")
		}
		for name, value := range first {
			if second[name] != value || value == "" || strings.ContainsAny(value, "\x00\r\n") || !ValidName(name) {
				t.Fatalf("round trip changed or admitted a bad setting")
			}
		}
	})
}
