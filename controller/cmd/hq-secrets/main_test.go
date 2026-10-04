package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

const sentinel = "sentinel.token.in-the-environment"

func environment(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func complete() map[string]string {
	return map[string]string{
		"SEVERINO_SECRETS_VAULT": "Example Vault", "SEVERINO_ENV_ITEM": "example env",
		"SEVERINO_CONNECT_CREDENTIAL": "op_connect_example", "OP_CONNECT_HOST": "http://127.0.0.1:880",
		"CREDENTIALS_DIRECTORY": "/run/credentials/example",
	}
}

func TestConfigurationRefusals(t *testing.T) {
	cases := map[string]struct {
		change func(map[string]string)
		euid   int
		args   []string
		want   string
	}{
		"a retired backend":           {func(e map[string]string) { e["SEVERINO_SECRETS_BACKEND"] = "service-account" }, 0, nil, "no longer exists"},
		"an unknown backend":          {func(e map[string]string) { e["SEVERINO_SECRETS_BACKEND"] = "unknown" }, 0, nil, "no longer exists"},
		"a Connect token in the env":  {func(e map[string]string) { e["OP_CONNECT_TOKEN"] = sentinel }, 0, nil, "Remove OP_CONNECT_TOKEN"},
		"a service token in the env":  {func(e map[string]string) { e["OP_SERVICE_ACCOUNT_TOKEN"] = sentinel }, 0, nil, "Remove OP_SERVICE_ACCOUNT_TOKEN"},
		"the legacy override":         {func(e map[string]string) { e["SEVERINO_CONTROLLER_ENV"] = "/run/severino-hq/severino_controller_env" }, 0, nil, "Remove SEVERINO_CONTROLLER_ENV"},
		"a container name as a flag":  {func(e map[string]string) { e["HQ_CONTAINER"] = "--privileged" }, 0, nil, "not a container name"},
		"not root":                    {func(map[string]string) {}, 10001, nil, "must run as root"},
		"no vault":                    {func(e map[string]string) { delete(e, "SEVERINO_SECRETS_VAULT") }, 0, nil, "SEVERINO_SECRETS_VAULT is required"},
		"no environment item":         {func(e map[string]string) { delete(e, "SEVERINO_ENV_ITEM") }, 0, nil, "SEVERINO_ENV_ITEM is required"},
		"no credential name":          {func(e map[string]string) { delete(e, "SEVERINO_CONNECT_CREDENTIAL") }, 0, nil, "SEVERINO_CONNECT_CREDENTIAL is required"},
		"no endpoint":                 {func(e map[string]string) { delete(e, "OP_CONNECT_HOST") }, 0, nil, "IPv4 loopback"},
		"a remote endpoint":           {func(e map[string]string) { e["OP_CONNECT_HOST"] = "https://example.com" }, 0, nil, "IPv4 loopback"},
		"a tailnet endpoint":          {func(e map[string]string) { e["OP_CONNECT_HOST"] = "http://192.0.2.10:8080" }, 0, nil, "IPv4 loopback"},
		"a port any account can bind": {func(e map[string]string) { e["OP_CONNECT_HOST"] = "http://127.0.0.1:8080" }, 0, nil, "only root can listen there"},
		"the first unprivileged port": {func(e map[string]string) { e["OP_CONNECT_HOST"] = "http://127.0.0.1:1024" }, 0, nil, "below 1024"},
		"an argument":                 {func(map[string]string) {}, 0, []string{"render"}, "usage"},
		"an unknown flag":             {func(map[string]string) {}, 0, []string{"-fallback"}, "usage"},
		"a shared runtime directory":  {func(e map[string]string) { e["SEVERINO_CONTROLLER_SECRET_DIR"] = "/run/severino-hq" }, 0, nil, "Unsafe controller secret directory"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			env := complete()
			c.change(env)
			var stderr bytes.Buffer
			code := run(context.Background(), c.args, environment(env), c.euid, func() (int, error) { return 1024, nil }, &stderr)
			want := exitCodes["config"]
			if name == "a shared runtime directory" {
				want = exitCodes["host"]
			}
			if code != want {
				t.Fatalf("exit %d, wanted %d: %s", code, want, &stderr)
			}
			if !strings.Contains(stderr.String(), c.want) || !strings.Contains(stderr.String(), "event=secrets.render.failed") {
				t.Fatalf("the failure line does not say why: %s", &stderr)
			}
			if strings.Contains(stderr.String(), sentinel) {
				t.Fatalf("the failure line carries the token: %s", &stderr)
			}
		})
	}
}

func TestTheBackendSwitchAcceptsOnlyConnect(t *testing.T) {
	for _, backend := range []string{"", "connect"} {
		env := complete()
		env["SEVERINO_SECRETS_BACKEND"] = backend
		if _, _, err := configure(nil, environment(env), &bytes.Buffer{}); err != nil {
			t.Fatalf("backend %q was refused: %v", backend, err)
		}
	}
}

// A host configured for the shell renderer, once Connect is on a privileged
// port: what its drop-ins still set is either read or ignored, never refused.
func TestAHostConfiguredForTheShellRendererIsAccepted(t *testing.T) {
	env := complete()
	env["SEVERINO_SECRETS_BACKEND"] = "connect"
	env["SEVERINO_MCP_SECRET_REF"] = "op://Example Vault/Example MCP/credential"
	env["OP_CONFIG_DIR"] = "/run/severino-hq-op"
	config, _, err := configure(nil, environment(env), &bytes.Buffer{})
	if err != nil {
		t.Fatalf("the previous release's drop-ins were refused: %v", err)
	}
	if config.Vault != "Example Vault" || config.ConnectCredential != "op_connect_example" || config.Endpoint != "http://127.0.0.1:880" {
		t.Fatalf("configuration: %+v", config)
	}
}

func TestDefaultsAreTheHostsPaths(t *testing.T) {
	config, container, err := configure(nil, environment(complete()), &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	layout := config.Layout
	if layout.RuntimeDir != "/run/severino-hq-secrets" || layout.WebDir != "/run/severino-hq-secrets/web" ||
		layout.SecretDir != "/opt/apps/severino-hq/secrets" || layout.RootUID != 0 || layout.WebUID != 10001 || layout.WebGID != 10001 {
		t.Fatalf("layout: %+v", layout)
	}
	if container != "severino-hq" || config.MinAppVariables != 15 || !strings.HasSuffix(config.RegistryPath, "/hq/config/controller-connections.json") {
		t.Fatalf("defaults: %s %+v", container, config)
	}
}

func TestEveryFailureClassHasItsOwnExitCode(t *testing.T) {
	seen := map[int]string{0: "success"}
	for _, class := range []string{"internal", "config", "host", "busy", "connect_unavailable", "connect_denied", "connect_response", "content", "web_unhealthy"} {
		code, ok := exitCodes[class]
		if !ok || code == 0 {
			t.Fatalf("%s has no exit code", class)
		}
		if other, taken := seen[code]; taken {
			t.Fatalf("%s and %s share exit code %d", class, other, code)
		}
		seen[code] = class
	}
}
