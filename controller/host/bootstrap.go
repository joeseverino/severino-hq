package host

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
)

// HostConfig is everything about one host that the repository does not hold:
// which vault and items HQ's secrets come from, how the renderer reaches the
// Connect server on this machine, and which runner deploys. It lives at
// /etc/severino-hq/host.json; deploy/host.example.json shows its shape.
type HostConfig struct {
	Vault   string        `json:"vault"`
	EnvItem string        `json:"env_item"`
	Connect ConnectConfig `json:"connect"`
	Runner  RunnerConfig  `json:"runner"`
}

// ConnectConfig is the local 1Password Connect server the renderer reads.
type ConnectConfig struct {
	// Unit is the service that runs Connect, which the renderer is ordered
	// after: Connect unlocks on the first authenticated request.
	Unit string `json:"unit"`
	// URL is its loopback address.
	URL string `json:"url"`
	// Credential is the encrypted systemd credential holding the token.
	Credential string `json:"credential"`
}

// RunnerConfig is the Actions runner that deploys to this host.
type RunnerConfig struct {
	Unit string `json:"unit"`
	User string `json:"user"`
}

var (
	unitNamed  = regexp.MustCompile(`^[A-Za-z0-9:_.@-]+\.service$`)
	userNamed  = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)
	credNamed  = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)
	plainValue = regexp.MustCompile(`^[^"\\\n\r%$]+$`)
)

// ReadHostConfig reads and checks the host's configuration. A field it does
// not know, a value systemd or sudo would read as something else, or a
// Connect that is not on loopback is refused.
func ReadHostConfig(data []byte) (HostConfig, error) {
	var config HostConfig
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&config); err != nil {
		return HostConfig{}, fmt.Errorf("the host configuration is not readable: %w", err)
	}
	for name, value := range map[string]string{"vault": config.Vault, "env_item": config.EnvItem} {
		if !plainValue.MatchString(value) {
			return HostConfig{}, fmt.Errorf("%s must be set, on one line, without quotes, backslashes, %% or $", name)
		}
	}
	if !unitNamed.MatchString(config.Connect.Unit) || !unitNamed.MatchString(config.Runner.Unit) {
		return HostConfig{}, errors.New("connect.unit and runner.unit must be service unit names")
	}
	if !credNamed.MatchString(config.Connect.Credential) {
		return HostConfig{}, errors.New("connect.credential must be a credential name")
	}
	if !userNamed.MatchString(config.Runner.User) || config.Runner.User == "root" {
		return HostConfig{}, errors.New("runner.user must be an unprivileged account name")
	}
	address, err := url.Parse(config.Connect.URL)
	if err != nil || address.Scheme != "http" || address.Hostname() != "127.0.0.1" || address.Port() == "" ||
		address.Path != "" || address.User != nil || address.RawQuery != "" {
		return HostConfig{}, errors.New("connect.url must be http://127.0.0.1:PORT")
	}
	return config, nil
}

// Bootstrap writes what a host needs that only the host knows: the
// renderer's vault and Connect, the runner's identity and its one sudo rule.
// Run on a new host and whenever the host's configuration changes; a deploy
// installs everything else.
func (m *Machine) Bootstrap(ctx context.Context, config HostConfig) error {
	secrets := filepath.Join(m.Systemd, SecretsUnit+".d")
	runner := filepath.Join(m.Systemd, config.Runner.Unit+".d")
	files := map[string]string{
		filepath.Join(secrets, "10-estate.conf"): fmt.Sprintf(`# Rendered by hq-host bootstrap from /etc/severino-hq/host.json.
[Service]
Environment="SEVERINO_SECRETS_VAULT=%s"
Environment="SEVERINO_ENV_ITEM=%s"
`, config.Vault, config.EnvItem),
		filepath.Join(secrets, "20-connect.conf"): fmt.Sprintf(`# Rendered by hq-host bootstrap from /etc/severino-hq/host.json.
# After Connect, which unlocks on the first authenticated request.
[Unit]
Requires=%[1]s
After=%[1]s

[Service]
LoadCredentialEncrypted=
LoadCredentialEncrypted=%[2]s
Environment="SEVERINO_CONNECT_CREDENTIAL=%[2]s"
Environment="OP_CONNECT_HOST=%[3]s"
`, config.Connect.Unit, config.Connect.Credential, config.Connect.URL),
		filepath.Join(runner, "10-runner-identity.conf"): fmt.Sprintf(`# Rendered by hq-host bootstrap from /etc/severino-hq/host.json.
[Service]
User=%s
`, config.Runner.User),
	}
	for path, body := range files {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		if err := writeAtomic(path, []byte(body), 0o644); err != nil {
			return err
		}
	}
	// The mount and the unit that requires it are the repository's now; a
	// drop-in that restated them is not.
	if err := os.Remove(filepath.Join(secrets, "20-credential-mount.conf")); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := m.sudoRule(ctx, config.Runner.User); err != nil {
		return err
	}
	return m.run(ctx, "systemctl", "daemon-reload")
}

// sudoRule lets the runner's account deploy and nothing else. The image it
// names is checked as root, not by the rule: sudo-rs matches no pattern inside
// an argument, only a trailing *. Deploy takes exactly one argument, refuses
// any but a digest-pinned composition of this repository, and verifies its
// signature before it pulls. The rule is checked by visudo before it is
// installed: a broken file in sudoers.d stops sudo for every account.
func (m *Machine) sudoRule(ctx context.Context, user string) error {
	rule := fmt.Sprintf("# Rendered by hq-host bootstrap: the runner deploys, and may do nothing else as root.\n%s ALL=(root) NOPASSWD: %s deploy *\n",
		user, HostProgram)
	staged, err := os.CreateTemp(m.Sudoers, ".severino-hq-deploy.")
	if err != nil {
		return err
	}
	defer os.Remove(staged.Name())
	_, err = staged.WriteString(rule)
	if err == nil {
		err = staged.Chmod(0o440)
	}
	if closeErr := staged.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err := m.run(ctx, "visudo", "-c", "-q", "-f", staged.Name()); err != nil {
		return fmt.Errorf("visudo refused the deploy rule: %w", err)
	}
	return os.Rename(staged.Name(), filepath.Join(m.Sudoers, "severino-hq-deploy"))
}
