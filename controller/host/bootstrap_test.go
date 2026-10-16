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
	if _, err := ReadHostConfig([]byte(hostJSON)); err != nil {
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
		if _, err := ReadHostConfig([]byte(strings.Replace(hostJSON, change[0], change[1], 1))); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestBootstrapWritesWhatOnlyTheHostKnows(t *testing.T) {
	f := newFakeHost(t)
	config, _ := ReadHostConfig([]byte(hostJSON))
	stale := filepath.Join(f.machine.Systemd, SecretsUnit+".d", "20-credential-mount.conf")
	os.MkdirAll(filepath.Dir(stale), 0o755)
	os.WriteFile(stale, []byte("[Service]\nRuntimeDirectory=severino-hq-op\n"), 0o644)

	if err := f.machine.Bootstrap(context.Background(), config); err != nil {
		t.Fatal(err)
	}

	read := func(path string) string {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		return string(data)
	}
	estate := read(filepath.Join(f.machine.Systemd, SecretsUnit+".d", "10-estate.conf"))
	connect := read(filepath.Join(f.machine.Systemd, SecretsUnit+".d", "20-connect.conf"))
	identity := read(filepath.Join(f.machine.Systemd, "actions.runner.example.service.d", "10-runner-identity.conf"))
	rule := read(filepath.Join(f.machine.Sudoers, "severino-hq-deploy"))
	for text, want := range map[string]string{
		estate:   `Environment="SEVERINO_SECRETS_VAULT=Example Vault"`,
		connect:  "After=example-connect.service",
		identity: "User=deployer",
		rule:     "deployer ALL=(root) NOPASSWD: /usr/local/lib/severino-hq/scripts/deploy-image.sh\n",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("%q not in\n%s", want, text)
		}
	}
	if !strings.Contains(connect, "LoadCredentialEncrypted=\nLoadCredentialEncrypted=op_connect_example\n") ||
		!strings.Contains(connect, `Environment="OP_CONNECT_HOST=http://127.0.0.1:8080"`) {
		t.Errorf("connect:\n%s", connect)
	}
	if info, _ := os.Stat(filepath.Join(f.machine.Sudoers, "severino-hq-deploy")); info.Mode().Perm() != 0o440 {
		t.Errorf("the sudo rule is %v", info.Mode().Perm())
	}
	if _, err := os.Stat(stale); err == nil {
		t.Error("the drop-in that restated the mount was left")
	}
	f.before(t, "visudo -c -q -f", "systemctl daemon-reload")
}

func TestARuleVisudoRefusesIsNotInstalled(t *testing.T) {
	f := newFakeHost(t)
	f.fail = []string{"visudo"}
	config, _ := ReadHostConfig([]byte(hostJSON))

	if err := f.machine.Bootstrap(context.Background(), config); err == nil {
		t.Fatal("a refused rule was installed")
	}
	if entries, _ := os.ReadDir(f.machine.Sudoers); len(entries) != 0 {
		t.Errorf("sudoers.d holds %v", entries)
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
		w.Write(served)
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
