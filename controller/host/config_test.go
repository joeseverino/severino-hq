package host

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const hostJSON = `{
  "vault": "Example Vault",
  "env_item": "example env",
  "connect": {"unit": "example-connect.service", "url": "http://127.0.0.1:8080", "credential": "op_connect_example"},
  "runner": {"unit": "actions.runner.example.service", "user": "deployer"}
}`

func TestTheHostConfigurationIsReadStrictly(t *testing.T) {
	if _, err := ReadConfig([]byte(hostJSON)); err != nil {
		t.Fatalf("the example was refused: %v", err)
	}
	for name, change := range map[string][2]string{
		"an unknown field":       {`"vault"`, `"extra": 1, "vault"`},
		"a quote in a value":     {`"Example Vault"`, `"Example \" Vault"`},
		"a newline in a value":   {`"example env"`, `"example\nenv"`},
		"a dollar in a value":    {`"example env"`, `"example $env"`},
		"Connect off loopback":   {`127.0.0.1:8080`, `10.0.0.5:8080`},
		"Connect over https":     {`http://127.0.0.1`, `https://127.0.0.1`},
		"Connect with no port":   {`127.0.0.1:8080`, `127.0.0.1`},
		"a unit that is a path":  {`example-connect.service`, `../evil.service`},
		"root as the runner":     {`"deployer"`, `"root"`},
		"an account with spaces": {`"deployer"`, `"de ployer"`},
		"a credential with a /":  {`op_connect_example`, `op/connect`},
	} {
		if _, err := ReadConfig([]byte(strings.Replace(hostJSON, change[0], change[1], 1))); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

// A configuration two readers could read differently is refused: a name
// given twice, a name in another case, or a second document after it.
func TestAnAmbiguousHostConfigurationIsRefused(t *testing.T) {
	for name, text := range map[string]string{
		"a name given twice":   strings.Replace(hostJSON, `"vault"`, `"vault": "Other Vault", "vault"`, 1),
		"a name in other case": strings.Replace(hostJSON, `"vault"`, `"Vault"`, 1),
		"trailing data":        hostJSON + `]`,
		"a second document":    hostJSON + hostJSON,
	} {
		if _, err := ReadConfig([]byte(text)); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestTheVerifierIsThePinnedDigestOrNothing(t *testing.T) {
	good := []byte("the pinned cosign")
	sum := sha256.Sum256(good)
	served, attempts := good, 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		attempts++
		if attempts == 1 {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		_, _ = w.Write(served)
	}))
	defer server.Close()
	target := filepath.Join(t.TempDir(), "verifier", "cosign")

	if err := installVerifier(context.Background(), server.Client(), server.URL, target, hex.EncodeToString(sum[:])); err != nil {
		t.Fatalf("a good download after a failed attempt: %v", err)
	}
	if info, err := os.Stat(target); err != nil || info.Mode().Perm() != 0o755 {
		t.Fatalf("not installed executable: %v %v", info, err)
	}
	attempts = 1
	if err := installVerifier(context.Background(), server.Client(), server.URL, target, hex.EncodeToString(sum[:])); err != nil || attempts != 1 {
		t.Errorf("an installed verifier was fetched again (%d attempts, %v)", attempts, err)
	}

	os.Remove(target)
	served = []byte("something else")
	if err := installVerifier(context.Background(), server.Client(), server.URL, target, hex.EncodeToString(sum[:])); err == nil {
		t.Fatal("a download with the wrong digest was installed")
	}
	if entries, _ := os.ReadDir(filepath.Dir(target)); len(entries) != 0 {
		t.Errorf("left behind: %v", entries)
	}
}
