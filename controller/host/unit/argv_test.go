package unit

import (
	"slices"
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

// subsequence is whether every word of part appears in whole, in order.
func subsequence(part, whole []string) bool {
	for _, word := range whole {
		if len(part) > 0 && part[0] == word {
			part = part[1:]
		}
	}
	return len(part) == 0
}

// A unit's command, read the way docker's flag parser reads it: the operands
// are words of the command, never an option or an option's value, and a
// command holding an option the table does not know names nothing. The words
// arrive NUL-separated, as no argument can hold a NUL.
func FuzzOperands(f *testing.F) {
	tables := []options{dockerOptions, startOptions, stopOptions, runOptions, composeOptions}
	for _, each := range []string{
		"--context default start --detach-keys ctrl-x hermes",
		"-H unix:///run/docker.sock container stop -t 10 hermes",
		"--attach hermes", "-ai hermes twin", "--detach-keys ctrl-x hermes",
		"--signal=TERM -- hermes", "-t", "-- -t hermes",
		"--rm -it --name hermes example/hermes start", "-e A=1 --name hermes example/hermes",
		"--compatibility --project-directory /opt/apps/pocket-id up -d",
		"-f /opt/apps/example/compose.yml up -d web db", "--unknown hermes", "-",
	} {
		for table := range tables {
			f.Add(uint8(table), table != 0 && table != 4, strings.ReplaceAll(each, " ", "\x00"))
		}
	}
	f.Fuzz(func(t *testing.T, table uint8, interspersed bool, joined string) {
		o := tables[int(table)%len(tables)]
		args := strings.Split(joined, "\x00")
		operands, ok := o.operands(args, interspersed)
		// A run's name is the value of its --name, and nothing else.
		if named := runName(args); len(named) > 0 {
			given := slices.Contains(args, "--name="+named[0])
			for i := range len(args) - 1 {
				given = given || args[i] == "--name" && args[i+1] == named[0]
			}
			if len(named) > 1 || !given {
				t.Fatalf("run names %q from %q", named, args)
			}
		}
		if !ok {
			return
		}
		if !subsequence(operands, args) || !interspersed && !slices.Equal(operands, args[len(args)-len(operands):]) {
			t.Fatalf("operands %q are not %q's own", operands, args)
		}
		if !slices.Contains(args, "--") {
			for i, operand := range operands {
				if (interspersed || i == 0) && len(operand) > 1 && strings.HasPrefix(operand, "-") {
					t.Fatalf("option %q read as an operand of %q", operand, args)
				}
			}
		}
		// An option the table knows, and a value after one that takes it,
		// change nothing of what the rest names.
		var known [][]string
		if len(o.flags) > 0 {
			known = append(known, []string{o.flags[0]})
		}
		if len(o.values) > 0 {
			known = append(known, []string{o.values[0], "hq-fuzz-value"}, []string{o.values[len(o.values)-1] + "=hq-fuzz-value"})
		}
		for _, prefix := range known {
			again, stillOK := o.operands(append(slices.Clone(prefix), args...), interspersed)
			if !stillOK || !slices.Equal(again, operands) {
				t.Fatalf("%q before %q changed its operands from %q to %q", prefix, args, operands, again)
			}
		}
	})
}
