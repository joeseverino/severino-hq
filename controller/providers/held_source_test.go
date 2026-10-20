package providers

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A kind read from a file this controller was given has a source when the
// file was mounted, and when a credentialed connection of its provider exists.
func TestAHeldReadingIsASourceBesideAConnection(t *testing.T) {
	declared := runtime.ControllerRegistry{
		Observations: map[string]string{
			string(runtime.ResourceKindTailscaleDevice): "tailscale",
			string(runtime.ResourceKindHostFirewall):    "host",
		},
		ConnectionCredentials: []runtime.ConnectionProvider{"tailscale"},
	}
	cases := []struct {
		kind      runtime.ResourceKind
		env       runtime.Environment
		connected map[runtime.ConnectionProvider]bool
		want      bool
	}{
		{runtime.ResourceKindTailscaleDevice, runtime.Environment{}, nil, false},
		{runtime.ResourceKindTailscaleDevice, runtime.Environment{TailnetStatus: "/run/status.json"}, nil, true},
		{runtime.ResourceKindTailscaleDevice, runtime.Environment{}, map[runtime.ConnectionProvider]bool{"tailscale": true}, true},
		{runtime.ResourceKindHostFirewall, runtime.Environment{}, map[runtime.ConnectionProvider]bool{"tailscale": true}, false},
		{runtime.ResourceKindHostFirewall, runtime.Environment{HostFirewall: "/run/firewall.json"}, nil, true},
	}
	for _, c := range cases {
		controller := NewController(New(c.env, supplied(), &fakeHTTP{}), declared)
		if got := controller.hasSource(string(c.kind), c.connected); got != c.want {
			t.Errorf("%s with %v and %v: source %v, want %v", c.kind, c.env, c.connected, got, c.want)
		}
	}
}

// A kind only ever read from what is held ignores every connection.
func TestAnOnlyHeldReadingIgnoresConnections(t *testing.T) {
	kind := string(runtime.ResourceKindGitHubProfile)
	declared := runtime.ControllerRegistry{
		Observations:          map[string]string{kind: "github"},
		ConnectionCredentials: []runtime.ConnectionProvider{"github"},
	}
	connected := map[runtime.ConnectionProvider]bool{"github": true}
	if NewController(New(runtime.Environment{}, supplied(), &fakeHTTP{}), declared).hasSource(kind, connected) {
		t.Error("a GitHub connection is not a profile's source")
	}
}

func TestAHeldSourceIsDeclaredOncePerKind(t *testing.T) {
	r := New(runtime.Environment{}, supplied(), &fakeHTTP{})
	defer func() {
		if recover() == nil {
			t.Error("a second held source for one kind was accepted")
		}
	}()
	r.readsHeld(runtime.ResourceKindHostFirewall, func() bool { return true })
}

// Every reading the launcher hands a run is the source of a kind of its own.
// A run that was not handed it then reads that kind as unconnected or failed,
// which leaves the last reading standing, instead of folding "not handed" into
// another kind's record as an empty value that replaces the last good one.
func TestEveryHostReadingIsAHeldSource(t *testing.T) {
	handed := launcherReadings(t)
	if len(handed) < 4 {
		t.Fatalf("found only %v in the launcher's readings", handed)
	}
	for _, name := range handed {
		env, err := runtime.ReadEnvironment([]string{name + "=/run/severino-hq/reading"})
		if err != nil {
			t.Fatal(err)
		}
		r := New(env, supplied(), &fakeHTTP{})
		held := false
		for _, source := range r.held {
			held = held || source.has()
		}
		if !held {
			t.Errorf("the launcher hands %s, and no kind is read from what it names", name)
		}
	}
}

// launcherReadings is each SEVERINO_ variable the launcher's readings set, as
// its source spells them.
func launcherReadings(t *testing.T) []string {
	t.Helper()
	path := filepath.Join("..", "host", "controller.go")
	parsed, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	variable := regexp.MustCompile(`^"(SEVERINO_[A-Z_]+)=`)
	var found []string
	for _, declaration := range parsed.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if !ok || function.Name.Name != "readings" {
			continue
		}
		ast.Inspect(function, func(node ast.Node) bool {
			if literal, ok := node.(*ast.BasicLit); ok && literal.Kind == token.STRING {
				if match := variable.FindStringSubmatch(literal.Value); match != nil {
					found = append(found, match[1])
				}
			}
			return true
		})
	}
	return found
}

// The dispatcher asks what each provider declared beside its reader; it names
// no kind of its own.
func TestTheDispatcherNamesNoKind(t *testing.T) {
	source, err := os.ReadFile("controller.go")
	if err != nil {
		t.Fatal(err)
	}
	if named := regexp.MustCompile(`runtime\.ResourceKind[A-Z]\w+`).FindAll(source, -1); len(named) > 0 {
		t.Errorf("controller.go names kinds: %s", named)
	}
}

// No provider reads a connection setting by a name it spells: a connection is
// a typed value handed to the provider it is for, and the environment is a
// struct of named facts. Each rule here fails on the way back to a lookup by
// string, which is how one provider could read another's credential.
func TestNoProviderReadsASettingByName(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil || len(files) == 0 {
		t.Fatalf("no provider sources: %v", err)
	}
	checked := 0
	for _, file := range files {
		if strings.HasSuffix(file, "_test.go") {
			continue
		}
		checked++
		parsed, err := parser.ParseFile(token.NewFileSet(), file, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(parsed, func(node ast.Node) bool {
			switch found := node.(type) {
			case *ast.IndexExpr:
				// r.Env[...] or r.Supplied[...]: a setting looked up by key.
				if held, ok := found.X.(*ast.SelectorExpr); ok && (held.Sel.Name == "Env" || held.Sel.Name == "Supplied") {
					t.Errorf("%s indexes %s by a key", file, held.Sel.Name)
				}
			case *ast.SelectorExpr:
				owner, _ := found.X.(*ast.Ident)
				switch {
				case owner != nil && owner.Name == "os" && (found.Sel.Name == "Getenv" || found.Sel.Name == "LookupEnv" || found.Sel.Name == "Environ"):
					t.Errorf("%s reads the process environment itself (os.%s)", file, found.Sel.Name)
				case found.Sel.Name == "WithSettings":
					t.Errorf("%s names a connection's settings by string (WithSettings)", file)
				}
			}
			return true
		})
	}
	if checked < 20 {
		t.Fatalf("only %d provider sources were checked", checked)
	}
	// What a provider is handed has no member a setting could be looked up in.
	for _, held := range []reflect.Type{reflect.TypeFor[runtime.Environment](), reflect.TypeFor[connections.Connection]()} {
		for field := range held.Fields() {
			if field.IsExported() && (field.Type.Kind() == reflect.Map || field.Type.Kind() == reflect.Interface) {
				t.Errorf("%s.%s is a %s", held.Name(), field.Name, field.Type.Kind())
			}
		}
	}
}
