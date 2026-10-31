package unit

import (
	"strings"
	"testing"
)

// A command names a container only where docker reads one: every other
// word of it, an option's value, an image, the program it runs, is not a
// name, and a command with an option docker's table here does not hold
// names nothing.
func TestOnlyTheOperandDockerReadsANameFromCounts(t *testing.T) {
	for _, each := range []struct {
		command       string
		names, others []string
	}{
		{"/usr/bin/docker start --attach hermes", []string{"hermes"}, []string{"start", "attach", "--attach"}},
		{"/usr/bin/docker start -ai hermes twin", []string{"hermes", "twin"}, []string{"ai", "-ai"}},
		{"/usr/bin/docker --context default start --detach-keys ctrl-x hermes", []string{"hermes"}, []string{"default", "ctrl-x"}},
		{"/usr/bin/docker -H unix:///run/docker.sock container stop -t 10 hermes", []string{"hermes"}, []string{"container", "10"}},
		{"/usr/bin/docker restart --signal=TERM -- hermes", []string{"hermes"}, []string{"TERM", "--"}},
		{"/usr/bin/docker run --rm -it --name hermes example/hermes start", []string{"hermes"}, []string{"run", "example/hermes", "start"}},
		{"/usr/bin/docker create --name=hermes example/hermes", []string{"hermes"}, []string{"create"}},
		{"/usr/bin/docker run -e A=1 --name hermes example/hermes", nil, []string{"hermes", "A=1"}},
		{"/usr/bin/docker run example/hermes --name hermes", nil, []string{"hermes"}},
		{"/usr/bin/docker start --unknown hermes", nil, []string{"hermes"}},
		{"/usr/bin/docker --unknown start hermes", nil, []string{"hermes", "start"}},
		{"/usr/bin/docker logs hermes", nil, []string{"hermes"}},
		{"/usr/bin/docker start --detach-keys", nil, []string{"--detach-keys"}},
		{"/usr/bin/docker compose --compatibility --project-directory /opt/apps/pocket-id up -d", nil, []string{"pocket-id", "up"}},
		{"/usr/bin/docker compose --project-directory /opt/apps/pocket-id down", nil, []string{"pocket-id"}},
		{"/usr/bin/docker compose --project-directory /opt/apps/pocket-id up -d", []string{"pocket-id"}, []string{"up", "-d", "compose"}},
		{"/usr/bin/docker compose --project-directory=/opt/apps/oauth2-proxy up -d", []string{"oauth2-proxy-oauth2-proxy-1", "oauth2-proxy-oauth2-proxy-12"},
			[]string{"oauth2-proxy", "oauth2-proxy-oauth2-proxy-", "oauth2-proxy-oauth2-proxy-1x", "oauth2-proxy-other-1"}},
		{"/usr/bin/test -s hermes", nil, []string{"hermes", "test"}},
	} {
		m := estate(t)
		runner := &Runner{EUID: 0, Command: m.command}
		starts := runner.starter(t.Context(), strings.Fields(each.command))
		for _, name := range each.names {
			if !starts(name) {
				t.Errorf("%q does not name %q", each.command, name)
			}
		}
		for _, name := range each.others {
			if starts(name) {
				t.Errorf("%q names %q", each.command, name)
			}
		}
	}
}

// A compose command that names its services brings up only theirs.
func TestAComposeCommandThatNamesServicesStartsOnlyTheirs(t *testing.T) {
	m := estate(t)
	m.projects["-f /opt/apps/example/compose.yml"] = `{"name":"example","services":{"web":{},"worker":{},"db":{"container_name":"example-db"}}}`
	runner := &Runner{EUID: 0, Command: m.command}
	starts := runner.starter(t.Context(), strings.Fields("/usr/bin/docker compose -f /opt/apps/example/compose.yml up -d web db"))
	for name, want := range map[string]bool{"example-web-1": true, "example-db": true, "example-worker-1": false, "example-db-1": false} {
		if starts(name) != want {
			t.Errorf("%s: %v", name, !want)
		}
	}
}
