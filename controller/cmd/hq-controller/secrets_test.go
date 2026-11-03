package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A pass whose bridge refuses it with a connection's secret in the refusal,
// in any form, prints and logs none of it: the binary learns the secret from
// the connections document it is handed.
func TestASecretInARefusalNeverLeavesTheBinary(t *testing.T) {
	binary, err := built()
	if err != nil {
		t.Fatal(err)
	}
	secret := `sentinel "binary" token/0123+&`
	document, err := connections.Document{SchemaVersion: connections.SchemaVersion, Connections: []connections.Connection{{
		Ref: "example", Provider: "example", Store: connections.Store{Vault: "example-vault", Item: "example-item"},
		ServiceAccount: &connections.ServiceAccount{APIToken: secret},
	}}}.Encode()
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp("", "hqs")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	held := filepath.Join(dir, "connections.json")
	if err := os.WriteFile(held, document, 0o600); err != nil {
		t.Fatal(err)
	}
	encoded := base64.StdEncoding.EncodeToString([]byte(secret))
	quoted := strconv.Quote(secret)
	for name, form := range map[string][2]string{
		"plain":        {"token " + secret + " refused", secret},
		"base64":       {"Basic " + encoded + " refused", encoded[2 : len(encoded)-2]},
		"url-encoded":  {"GET /v1?token=" + url.QueryEscape(secret), url.QueryEscape(secret)},
		"quote-escape": {"token " + quoted + " refused", quoted[1 : len(quoted)-1]},
	} {
		t.Run(name, func(t *testing.T) {
			socket := filepath.Join(dir, "bridge.sock")
			listener, err := net.Listen("unix", socket)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(socket, 0o600); err != nil {
				t.Fatal(err)
			}
			server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/problem+json")
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(runtime.Problem{Status: http.StatusBadRequest, Title: "Refused", Detail: form[0]})
			})}
			go func() { _ = server.Serve(listener) }()
			defer server.Close()
			command := exec.Command(binary, "--apply")
			command.Env = []string{"PATH=" + os.Getenv("PATH"), BridgeSocket + "=" + socket, "HQ_CONTROLLER_ID=e2e", "HQ_CONTROLLER_CONNECTIONS=" + held}
			var stdout, stderr bytes.Buffer
			command.Stdout, command.Stderr = &stdout, &stderr
			if err := command.Run(); err == nil {
				t.Fatalf("a refused pass exited 0: %s", stdout.String())
			}
			if !strings.Contains(stdout.String(), "bridge failed") {
				t.Fatalf("the refusal was not reported: %s", stdout.String())
			}
			for where, text := range map[string]string{"stdout": stdout.String(), "stderr": stderr.String()} {
				if strings.Contains(text, form[1]) {
					t.Errorf("%s carries the secret: %s", where, text)
				}
			}
		})
	}
}
