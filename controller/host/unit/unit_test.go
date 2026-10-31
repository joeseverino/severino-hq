package unit

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"

	"golang.org/x/sys/unix"
)

// container is one container as docker would list it.
type container struct {
	name, unit, workingDir string
}

// machine is a stand-in for docker and systemd on one host, answering in
// the forms each prints.
type machine struct {
	t          *testing.T
	containers []container
	// units holds what `systemctl show` prints for each loaded unit.
	units map[string]string
	// projects holds what `docker compose <options> config --format json`
	// prints for each set of project options.
	projects map[string]string
	fails    bool
	calls    []string
}

// show is what systemctl show prints for a unit with these commands.
func show(id string, commands ...string) string {
	lines := []string{"Id=" + id, "LoadState=loaded"}
	for _, command := range commands {
		property, argv, _ := strings.Cut(command, " ")
		lines = append(lines, fmt.Sprintf("%s={ path=%s ; argv[]=%s ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }",
			property, strings.Fields(argv)[0], argv))
	}
	return strings.Join(lines, "\n") + "\n"
}

func answer(ctx context.Context, out string, code int) *exec.Cmd {
	return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
}

func (m *machine) command(ctx context.Context, name string, args ...string) *exec.Cmd {
	m.calls = append(m.calls, strings.Join(append([]string{name}, args...), " "))
	switch {
	case name == "docker" && slices.Equal(args[:3], []string{"container", "ls", "--all"}):
		filter := args[slices.Index(args, "--filter")+1]
		format := args[slices.Index(args, "--format")+1]
		var out strings.Builder
		for _, each := range m.containers {
			if filter == "label="+Label+"="+each.unit {
				names, _ := json.Marshal(each.name)
				out.WriteString(string(names))
				// The label compose sets to its project's directory, which
				// whoever creates a container can copy, when it is asked for.
				if strings.Contains(format, "com.docker.compose.project.working_dir") {
					dir, _ := json.Marshal(each.workingDir)
					out.WriteString(" " + string(dir))
				}
				out.WriteString("\n")
			}
		}
		return answer(ctx, out.String(), 0)
	case name == "docker" && args[0] == "compose" && slices.Equal(args[len(args)-3:], []string{"config", "--format", "json"}):
		if config, ok := m.projects[strings.Join(args[1:len(args)-3], " ")]; ok {
			return answer(ctx, config, 0)
		}
		return answer(ctx, "", 1)
	case name == "systemctl" && args[0] == "show":
		unit := args[len(args)-1]
		if shown, ok := m.units[unit]; ok {
			return answer(ctx, shown, 0)
		}
		return answer(ctx, "Id="+unit+"\nLoadState=not-found\n", 0)
	case name == "systemctl" && slices.Contains(Verbs, args[0]):
		if m.fails {
			return answer(ctx, "Job for hermes.service failed.", 1)
		}
		return answer(ctx, "", 0)
	}
	m.t.Errorf("ran %s %v", name, args)
	return answer(ctx, "", 127)
}

// ask sends one request line as the euid given and reads the answer.
func (m *machine) ask(euid int, line string) Result {
	m.t.Helper()
	runner := &Runner{EUID: euid, Command: m.command}
	return runner.Serve(context.Background(), strings.NewReader(line))
}

func (m *machine) acted() bool {
	return slices.ContainsFunc(m.calls, func(call string) bool {
		return slices.ContainsFunc(Verbs, func(verb string) bool { return strings.HasPrefix(call, "systemctl "+verb+" ") })
	})
}

// estate is a host like the one the label was made for: hermes started by
// its own unit by name, and two compose projects one unit brings up, one
// whose service names its container and one that leaves compose to, beside a
// unit that has nothing to do with Docker.
func estate(t *testing.T) *machine {
	return &machine{
		t: t,
		containers: []container{
			{name: "hermes", unit: "hermes.service", workingDir: "/opt/apps/hermes"},
			{name: "pocket-id", unit: "example-apps.service", workingDir: "/opt/apps/pocket-id"},
			{name: "oauth2-proxy-oauth2-proxy-1", unit: "example-apps.service", workingDir: "/opt/apps/oauth2-proxy"},
		},
		projects: map[string]string{
			"-f /opt/apps/hermes/docker-compose.yml":     `{"name":"hermes","services":{"hermes":{"container_name":"hermes","image":"example/hermes"}}}`,
			"--project-directory /opt/apps/pocket-id":    `{"name":"pocket-id","services":{"pocket-id":{"container_name":"pocket-id","image":"example/pocket-id"}}}`,
			"--project-directory=/opt/apps/oauth2-proxy": `{"name":"oauth2-proxy","services":{"oauth2-proxy":{"image":"example/oauth2-proxy"}}}`,
		},
		units: map[string]string{
			"hermes.service": show("hermes.service",
				"ExecStartPre /usr/bin/test -s /run/hermes/identity.json",
				"ExecStartPre /usr/bin/docker compose -f /opt/apps/hermes/docker-compose.yml create --no-build hermes",
				"ExecStart /usr/bin/docker start --attach hermes"),
			"example-apps.service": show("example-apps.service",
				"ExecStart /usr/bin/docker compose --project-directory /opt/apps/pocket-id up -d",
				"ExecStart /usr/bin/docker compose --project-directory=/opt/apps/oauth2-proxy up -d"),
			"ssh.service": show("ssh.service", "ExecStart /usr/sbin/sshd -D"),
		},
	}
}

func TestEachVerbActsOnALabeledUnitThatStartsItsContainers(t *testing.T) {
	for _, unit := range []string{"hermes.service", "example-apps.service"} {
		for _, verb := range Verbs {
			m := estate(t)
			result := m.ask(0, Request(verb, unit)+"\n")
			if result.Outcome != Done || result.Unit != unit || result.Verb != verb {
				t.Fatalf("%s %s: %+v", verb, unit, result)
			}
			if !slices.Contains(m.calls, "systemctl "+verb+" -- "+unit) {
				t.Errorf("%s %s ran %v", verb, unit, m.calls)
			}
		}
	}
}

func TestAUnitNoContainerNamesIsRefused(t *testing.T) {
	m := estate(t)
	result := m.ask(0, "stop ssh.service\n")
	if result.Outcome != Refused || m.acted() {
		t.Fatalf("%+v, calls %v", result, m.calls)
	}
}

func TestALabelNamingAUnitThatDoesNotStartTheContainerIsRefused(t *testing.T) {
	// Containers anyone with Docker could make, each naming a unit that never
	// runs it: by a name no command gives, by a name that is some other word
	// of the unit's commands, by copying a project's directory label, or by
	// a name in a project's pattern for a service that is not there or
	// names its container itself.
	for _, planted := range []container{
		{name: "planted", unit: "ssh.service"},
		{name: "stowaway", unit: "hermes.service", workingDir: "/srv/elsewhere"},
		{name: "start", unit: "hermes.service"},
		{name: "compose", unit: "hermes.service"},
		{name: "create", unit: "hermes.service"},
		{name: "stowaway", unit: "hermes.service", workingDir: "/opt/apps/hermes"},
		{name: "compose", unit: "example-apps.service"},
		{name: "up", unit: "example-apps.service"},
		{name: "stowaway", unit: "example-apps.service", workingDir: "/opt/apps/pocket-id"},
		{name: "stowaway", unit: "example-apps.service", workingDir: "/opt/apps/oauth2-proxy"},
		{name: "oauth2-proxy-stowaway-1", unit: "example-apps.service", workingDir: "/opt/apps/oauth2-proxy"},
		{name: "pocket-id-pocket-id-1", unit: "example-apps.service", workingDir: "/opt/apps/pocket-id"},
	} {
		m := estate(t)
		m.containers = append(m.containers, planted)
		result := m.ask(0, "stop "+planted.unit)
		if result.Outcome != Refused || !strings.Contains(result.Reason, "does not start "+planted.name) {
			t.Errorf("%+v: %+v", planted, result)
		}
		if m.acted() {
			t.Errorf("%+v: systemd was asked to act: %v", planted, m.calls)
		}
	}
}

func TestAnAliasCannotStandInForItsUnit(t *testing.T) {
	m := estate(t)
	// systemd shows an alias under the Id of the unit it names.
	m.containers = append(m.containers, container{name: "hermes-twin", unit: "alias.service", workingDir: "/opt/apps/hermes"})
	m.units["alias.service"] = m.units["hermes.service"]
	result := m.ask(0, "stop alias.service")
	if result.Outcome != Refused || !strings.Contains(result.Reason, "not a loaded unit") || m.acted() {
		t.Fatalf("%+v, calls %v", result, m.calls)
	}
}

func TestOnlyTheClosedGrammarIsRead(t *testing.T) {
	for _, line := range []string{
		"",
		"stop",
		"stop hermes.service extra",
		"stop  hermes.service",
		"stop\thermes.service",
		"kill hermes.service",
		"stop hermes",
		"stop hermes.socket",
		"stop getty@tty1.service",
		"stop ../hermes.service",
		"stop --now",
		"stop -hermes.service",
		"stop hermes.service\r",
		"stop " + strings.Repeat("a", RequestLimit) + ".service",
	} {
		m := estate(t)
		result := m.ask(0, line)
		if result.Outcome != Refused || len(m.calls) != 0 {
			t.Errorf("%q: %+v, calls %v", line, result, m.calls)
		}
	}
}

func TestOnlyRootActs(t *testing.T) {
	m := estate(t)
	result := m.ask(1000, "stop hermes.service")
	if result.Outcome != Refused || len(m.calls) != 0 {
		t.Fatalf("%+v, calls %v", result, m.calls)
	}
}

func TestASystemdFailureIsReportedWithItsLine(t *testing.T) {
	m := estate(t)
	m.fails = true
	result := m.ask(0, "start hermes.service")
	if result.Outcome != Failed || result.Reason != "Job for hermes.service failed." {
		t.Fatalf("%+v", result)
	}
}

func TestServeAnswersOneLineOfJSONAndOnlyServes(t *testing.T) {
	var out strings.Builder
	if code := Main(t.Context(), []string{"--serve"}, 1000, strings.NewReader("stop hermes.service\n"), &out); code != 0 {
		t.Fatalf("exit %d", code)
	}
	var result Result
	if lines := strings.Split(strings.TrimSuffix(out.String(), "\n"), "\n"); len(lines) != 1 || json.Unmarshal([]byte(lines[0]), &result) != nil || result.Outcome != Refused {
		t.Fatalf("answered %q", out.String())
	}
	for _, args := range [][]string{{}, {"stop", "hermes.service"}, {"--serve", "--stdio"}} {
		out.Reset()
		if code := Main(t.Context(), args, 0, strings.NewReader(""), &out); code != 2 {
			t.Errorf("%v: exit %d", args, code)
		}
	}
}

// accepted is a connection the way systemd's StandardInput=socket hands it
// over: an accepted Unix socket in blocking mode, opened as stdin is, beside
// the client's end.
func accepted(t *testing.T) (client net.Conn, stdin *os.File) {
	t.Helper()
	// Short, since a socket path is bounded at about a hundred bytes.
	dir, err := os.MkdirTemp("/tmp", "unit")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	listener, err := net.Listen("unix", filepath.Join(dir, "unit.sock"))
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	client, err = net.Dial("unix", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { client.Close() })
	conn, err := listener.Accept()
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	file, err := conn.(*net.UnixConn).File()
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	// Fd leaves the descriptor blocking; a dup of it opened by NewFile is
	// not pollable, as os.Stdin is not.
	fd, err := unix.Dup(int(file.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	stdin = os.NewFile(uintptr(fd), "stdin")
	t.Cleanup(func() { stdin.Close() })
	return client, stdin
}

func nonblocking(t *testing.T, file *os.File) bool {
	t.Helper()
	flags, err := unix.FcntlInt(file.Fd(), unix.F_GETFL, 0)
	if err != nil {
		t.Fatal(err)
	}
	return flags&unix.O_NONBLOCK != 0
}

// Reading the peer leaves the connection as systemd handed it, so a request
// that arrives after the check is still waited for and read.
func TestAdmittingASocketLeavesItBlockingForTheRequest(t *testing.T) {
	client, stdin := accepted(t)
	if nonblocking(t, stdin) {
		t.Fatal("the connection was not handed over blocking")
	}
	// This test's account is refused unless it is root; what matters here is
	// what reading the peer leaves behind.
	_ = admit(stdin)
	if nonblocking(t, stdin) {
		t.Fatal("reading the peer made the connection nonblocking")
	}
	m := estate(t)
	in := &firstRead{File: stdin, started: make(chan struct{})}
	served := make(chan Result)
	go func() {
		runner := &Runner{EUID: 0, Command: m.command}
		served <- runner.Serve(t.Context(), in)
	}()
	<-in.started
	if _, err := client.Write([]byte("stop hermes.service\n")); err != nil {
		t.Fatal(err)
	}
	if result := <-served; result.Outcome != Done {
		t.Fatalf("%+v", result)
	}
}

// firstRead says when its reader first asks for the request, so the request
// can be written after the read has begun.
type firstRead struct {
	*os.File
	started chan struct{}
	once    sync.Once
}

func (r *firstRead) Read(p []byte) (int, error) {
	r.once.Do(func() { close(r.started) })
	return r.File.Read(p)
}

// A socket's peer is the boundary: one from this test's own account, which
// is neither root nor HQ's, is refused before the request is read.
func TestASocketPeerOfAnotherAccountIsRefused(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("run as root, the peer is root")
	}
	_, file := accepted(t)
	if err := admit(file); err == nil {
		t.Fatal("a peer of another account was admitted")
	}
	if err := admit(strings.NewReader("stop hermes.service")); err != nil {
		t.Fatalf("a carrier that is not a socket was refused: %v", err)
	}
}
