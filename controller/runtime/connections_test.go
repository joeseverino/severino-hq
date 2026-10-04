package runtime

import (
	"strings"
	"testing"
)

func TestConnectionResolutionRefusesAmbiguity(t *testing.T) {
	env := Environment{"A_CONNECTION_REF": "example-a", "A_PROVIDER": "example", "B_CONNECTION_REF": "example-b", "B_PROVIDER": "example"}
	if _, err := env.Prefix("example", ""); err == nil {
		t.Fatal("ambiguous provider accepted")
	}
	if prefix, err := env.Prefix("example", "example-b"); err != nil || prefix != "B" {
		t.Fatalf("%s %v", prefix, err)
	}
	if _, err := env.Prefix("example", "absent"); err == nil {
		t.Fatal("unknown connection accepted")
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
			if _, err := env.SSH("example"); err == nil {
				t.Errorf("accepted %s=%q", field, value)
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
	if err == nil || strings.Contains(err.Error(), "PRIVATE_PROVIDER") {
		t.Fatal(err)
	}
}
