package host

import (
	"bufio"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const onFailureDropIn = "10-on-failure.conf"

// shippedUnits is the repository's own unit directory.
var shippedUnits = filepath.Join("..", "..", "deploy", "systemd")

// unitValue is the last value key takes in one file, as systemd reads a
// repeated key.
func unitValue(t *testing.T, file, key string) string {
	t.Helper()
	f, err := os.Open(file)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var value string
	lines := bufio.NewScanner(f)
	for lines.Scan() {
		if v, ok := strings.CutPrefix(lines.Text(), key+"="); ok {
			value = v
		}
	}
	if err := lines.Err(); err != nil {
		t.Fatal(err)
	}
	return value
}

// unitRuleBreaks is every way the units shipped in dir break the rules that
// keep a drop-in from silently replacing a unit's command, and that make every
// failure reach HQ without one starting itself again.
func unitRuleBreaks(t *testing.T, dir string) []string {
	t.Helper()
	shipped, err := Shipped(dir)
	if err != nil {
		t.Fatal(err)
	}
	read := func(name, key string) string {
		return unitValue(t, filepath.Join(dir, name), key)
	}
	breaks := dropInCommandBreaks(read, shipped)
	return append(breaks, failureBreaks(t, dir, read, shipped)...)
}

// A drop-in wins, so one that replaces ExecStart with anything but what the
// unit runs hides every later change to the unit's command.
func dropInCommandBreaks(read func(name, key string) string, shipped []string) []string {
	var breaks []string
	for _, name := range shipped {
		unit, _, isDropIn := strings.Cut(name, ".d/")
		replaced := read(name, "ExecStart")
		if !isDropIn || replaced == "" {
			continue
		}
		if runs := read(unit, "ExecStart"); replaced != runs {
			breaks = append(breaks, fmt.Sprintf("%s runs %q, but %s runs %q", name, replaced, unit, runs))
		}
	}
	return breaks
}

// The shared drop-in reaches every unit whose name has the repository's
// prefix, so the rules are that every unit has that prefix, that only a unit a
// loop would follow replaces the drop-in (the one a failure starts, and each a
// shipped path starts, since the read it asks for rings that path's doorbell),
// and that no unit names a handler of its own beside it.
func failureBreaks(t *testing.T, dir string, read func(name, key string) string, shipped []string) []string {
	t.Helper()
	var breaks []string
	started := read("severino-hq-.service.d/"+onFailureDropIn, "OnFailure")
	template, _, _ := strings.Cut(started, "@")
	if !strings.HasPrefix(started, "severino-hq-") || !strings.HasSuffix(started, ".service") || !slices.Contains(shipped, template+"@.service") {
		breaks = append(breaks, fmt.Sprintf("a failure starts %q, which is not an instance of a shipped template", started))
	}
	loops := []string{started}
	for _, name := range shipped {
		if !strings.Contains(name, "/") && strings.HasSuffix(name, ".path") {
			loops = append(loops, read(name, "Unit"))
		}
	}
	for _, name := range shipped {
		if !strings.Contains(name, "/") {
			breaks = append(breaks, unitFailureBreaks(t, dir, read, name, started)...)
		}
	}
	exempt := map[string]bool{}
	for _, name := range shipped {
		unit, dropIn, _ := strings.Cut(name, ".d/")
		if dropIn != onFailureDropIn || strings.HasPrefix(unit, "severino-hq-.") {
			continue
		}
		exempt[unit] = true
		if read(name, "OnFailure") != "" {
			breaks = append(breaks, name+" starts a unit on failure")
		}
		if !slices.Contains(loops, unit) {
			breaks = append(breaks, unit+" is exempt from telling HQ it failed, and no loop requires that")
		}
	}
	for _, unit := range loops {
		if !exempt[unit] {
			breaks = append(breaks, fmt.Sprintf("%s would start again whenever it failed: it needs an empty %s.d/%s", unit, unit, onFailureDropIn))
		}
	}
	for _, name := range shipped {
		if !isSharedFailureDropIn(name) && read(name, "OnFailure") != "" {
			breaks = append(breaks, name+" sets OnFailure outside the shared drop-in")
		}
	}
	return breaks
}

func isSharedFailureDropIn(name string) bool {
	return strings.HasPrefix(name, "severino-hq-.") && strings.HasSuffix(name, ".d/"+onFailureDropIn)
}

// unitFailureBreaks is how one top-level unit could fail without HQ hearing.
func unitFailureBreaks(t *testing.T, dir string, read func(name, key string) string, name, started string) []string {
	t.Helper()
	kind := name[strings.LastIndex(name, ".")+1:]
	if kind == "mount" {
		// A mount's name is its path, so no prefix drop-in reaches it: it
		// fails through the unit that cannot start without it.
		return mountFailureBreaks(t, dir, read(name, "Where"), name)
	}
	if kind != "service" && kind != "timer" && kind != "path" && kind != "socket" {
		return []string{fmt.Sprintf("%s: no failure drop-in covers a %s", name, kind)}
	}
	if !strings.HasPrefix(name, "severino-hq-") {
		return []string{name + " is outside the prefix the failure drop-in covers"}
	}
	if read("severino-hq-."+kind+".d/"+onFailureDropIn, "OnFailure") != started {
		return []string{fmt.Sprintf("severino-hq-.%s.d/%s does not start %s", kind, onFailureDropIn, started)}
	}
	return nil
}

func mountFailureBreaks(t *testing.T, dir, where, name string) []string {
	t.Helper()
	services, _ := filepath.Glob(filepath.Join(dir, "severino-hq-*.service"))
	for _, service := range services {
		if unitValue(t, service, "RequiresMountsFor") == where {
			return nil
		}
	}
	return []string{fmt.Sprintf("%s: nothing that reports its failure requires %s", name, where)}
}

func TestTheShippedUnitsKeepTheirRules(t *testing.T) {
	for _, broken := range unitRuleBreaks(t, shippedUnits) {
		t.Error(broken)
	}
	for _, broken := range sudoUnitBreaks(t, shippedUnits) {
		t.Error(broken)
	}
}

// execsSudo is each controller file that runs sudo, and the hq-host
// subcommand that reaches it. TestOnlyTheseFilesRunSudo holds the list
// whole, so a new caller of sudo names the subcommand a unit must let run it.
var execsSudo = map[string]string{
	filepath.Join("host", "verify.go"): "verify",
	filepath.Join("host", "check.go"):  "check",
}

// sudoUnitBreaks is each shipped unit that runs a subcommand which execs
// sudo, under a setting that keeps sudo from starting: sudo-rs refuses to run
// with the no-new-privileges flag set, which NoNewPrivileges= sets and
// DynamicUser= implies (systemd.exec(5)). A drop-in's setting counts too.
func sudoUnitBreaks(t *testing.T, dir string) []string {
	t.Helper()
	shipped, err := Shipped(dir)
	if err != nil {
		t.Fatal(err)
	}
	var breaks []string
	for _, unit := range shipped {
		if strings.Contains(unit, "/") {
			continue
		}
		subcommand := hqHostSubcommand(unitValue(t, filepath.Join(dir, unit), "ExecStart"))
		if !slices.Contains(slices.Collect(maps.Values(execsSudo)), subcommand) {
			continue
		}
		kind := unit[strings.LastIndex(unit, "."):]
		for _, name := range shipped {
			owner, _, _ := strings.Cut(name, ".d/")
			if name != unit && owner != unit && owner != "severino-hq-"+kind {
				continue
			}
			for _, key := range []string{"NoNewPrivileges", "DynamicUser"} {
				if value := unitValue(t, filepath.Join(dir, name), key); systemdTrue(value) {
					breaks = append(breaks, fmt.Sprintf("%s sets %s=%s, but %s runs hq-host %s, which runs sudo, and sudo-rs refuses to start under no-new-privileges", name, key, value, unit, subcommand))
				}
			}
		}
	}
	return breaks
}

// hqHostSubcommand is the subcommand an ExecStart line gives hq-host, or ""
// for a line that runs another program.
func hqHostSubcommand(exec string) string {
	fields := strings.Fields(exec)
	for i, field := range fields[:max(len(fields)-1, 0)] {
		if filepath.Base(strings.TrimLeft(field, "@-:+!")) == "hq-host" {
			return fields[i+1]
		}
	}
	return ""
}

func systemdTrue(value string) bool {
	return slices.Contains([]string{"1", "yes", "y", "true", "t", "on"}, strings.ToLower(value))
}

func TestOnlyTheseFilesRunSudo(t *testing.T) {
	root := ".."
	found := map[string]bool{}
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return err
		}
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			return err
		}
		ast.Inspect(file, func(node ast.Node) bool {
			if call, ok := node.(*ast.CallExpr); ok {
				for _, arg := range call.Args {
					if literal, ok := arg.(*ast.BasicLit); ok && literal.Kind == token.STRING && literal.Value == `"sudo"` {
						relative, _ := filepath.Rel(root, path)
						found[relative] = true
					}
				}
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for file := range found {
		if _, listed := execsSudo[file]; !listed {
			t.Errorf("%s runs sudo: name it in execsSudo with the hq-host subcommand that reaches it", file)
		}
	}
	for file := range execsSudo {
		if _, runs := found[file]; !runs {
			t.Errorf("%s no longer runs sudo: drop it from execsSudo", file)
		}
	}
}

func TestAUnitThatKeepsSudoFromStartingIsNamed(t *testing.T) {
	for name, body := range map[string]string{
		"severino-hq-script-drift.service.d/20-harden.conf":  "[Service]\nNoNewPrivileges=yes\n",
		"severino-hq-.service.d/20-harden.conf":              "[Service]\nNoNewPrivileges=true\n",
		"severino-hq-script-drift.service.d/20-dynamic.conf": "[Service]\nDynamicUser=on\n",
	} {
		t.Run(name, func(t *testing.T) {
			dir := copyUnits(t)
			if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
				t.Fatal(err)
			}
			if breaks := sudoUnitBreaks(t, dir); len(breaks) != 1 || !strings.Contains(breaks[0], name) {
				t.Errorf("named %v, want %s alone", breaks, name)
			}
		})
	}
}

// copyUnits is the shipped unit directory, copied where a case may break it.
func copyUnits(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.CopyFS(dir, os.DirFS(shippedUnits)); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestAUnitThatBreaksARuleIsNamed(t *testing.T) {
	for _, c := range []struct {
		name  string
		write map[string]string
		drop  string
	}{
		{name: "a unit outside the prefix", write: map[string]string{"other.service": "[Service]\nExecStart=/bin/true\n"}},
		{name: "a handler of its own", write: map[string]string{"severino-hq-extra.service": "[Unit]\nOnFailure=other.service\n"}},
		{name: "a drop-in that replaces the command", write: map[string]string{"severino-hq-web.service.d/20-exec.conf": "[Service]\nExecStart=\nExecStart=/bin/true\n"}},
		{name: "an exemption no loop needs", write: map[string]string{"severino-hq-web.service.d/" + onFailureDropIn: "[Unit]\n"}},
		{name: "a loop with no exemption", drop: "severino-hq-controller.service.d/" + onFailureDropIn},
		{name: "a mount nothing requires", write: map[string]string{"run-example.mount": "[Mount]\nWhere=/run/example\n"}},
		{name: "a unit type no drop-in covers", write: map[string]string{"severino-hq-example.slice": "[Slice]\n"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			dir := copyUnits(t)
			for name, body := range c.write {
				if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			if c.drop != "" {
				if err := os.Remove(filepath.Join(dir, c.drop)); err != nil {
					t.Fatal(err)
				}
			}
			if breaks := unitRuleBreaks(t, dir); len(breaks) != 1 {
				t.Errorf("named %v, want the one rule broken", breaks)
			}
		})
	}
}

// Shipped leaves out what a person copies into place (a template would put
// example values into production) and the strays a checkout grows.
func TestShippedLeavesOutTemplatesAndStrays(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"a.service", "a.timer", "b.path", "a.service.d/10-x.conf",
		"a.service.d/20-y.conf.example", "connect.conf.example", "README", ".DS_Store", "nested/deeper/c.service"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}

	shipped, err := Shipped(dir)

	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"a.service", "a.service.d/10-x.conf", "a.timer", "b.path"}; !slices.Equal(shipped, want) {
		t.Errorf("shipped %v, want %v", shipped, want)
	}
}
