package runtime

import (
	"errors"
	"strings"
	"testing"
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

// Regression (Stage 1 M3): naming a connection skipped the provider check, so
// one vendor's credential could be sent to another.
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
