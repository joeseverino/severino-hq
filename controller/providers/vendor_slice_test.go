package providers

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
)

var vendorCalls = struct {
	sync.Mutex
	calls map[string]map[string]bool
}{calls: map[string]map[string]bool{}}

func recordVendorCall(vendor, method, path string) {
	vendorCalls.Lock()
	defer vendorCalls.Unlock()
	if vendorCalls.calls[vendor] == nil {
		vendorCalls.calls[vendor] = map[string]bool{}
	}
	vendorCalls.calls[vendor][method+" "+path] = true
}

func TestMain(m *testing.M) {
	code := m.Run()
	if flag.Lookup("test.run").Value.String() == "" && flag.Lookup("test.skip").Value.String() == "" && flag.Lookup("test.list").Value.String() == "" {
		if err := checkVendorOperations(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			code = 1
		}
	}
	os.Exit(code)
}

func checkVendorOperations() error {
	var missing []string
	for _, vendor := range []string{"cloudflare", "github"} {
		data, err := os.ReadFile("../api/vendor/" + vendor + "/openapi.slice.json")
		if err != nil {
			return err
		}
		var spec struct {
			Paths map[string]map[string]struct {
				OperationID string `json:"operationId"`
			} `json:"paths"`
		}
		if err := json.Unmarshal(data, &spec); err != nil {
			return err
		}
		reached := map[string]bool{}
		patterns := map[string]*regexp.Regexp{}
		for path, methods := range spec.Paths {
			pattern := regexp.QuoteMeta(path)
			// GitHub content paths and refs may contain slashes.
			for _, parameter := range []string{"path", "ref", "branch"} {
				pattern = strings.ReplaceAll(pattern, regexp.QuoteMeta("{"+parameter+"}"), `.+`)
			}
			pattern = regexp.MustCompile(`\\\{[^}]+\\\}`).ReplaceAllString(pattern, `[^/]+`)
			for method, op := range methods {
				patterns[strings.ToUpper(method)+" "+op.OperationID] = regexp.MustCompile("^" + strings.ToUpper(method) + " " + pattern + "$")
			}
		}
		for call := range vendorCalls.calls[vendor] {
			matched := false
			for op, pattern := range patterns {
				if pattern.MatchString(call) {
					reached[op] = true
					matched = true
				}
			}
			if !matched {
				return fmt.Errorf("%s call outside vendor slice: %s", vendor, call)
			}
		}
		for op := range patterns {
			if !reached[op] {
				missing = append(missing, vendor+" "+op)
			}
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("vendor operations without test coverage: %v", missing)
	}
	return nil
}

// Every generated declaration must connect to a provider reference through its
// field types, aliases or generated union methods. Fields contribute their type
// edges, including inline structs; this checks reachability, not runtime reads.
func TestVendorModelsReachProviderCode(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, vendor := range []string{"cfapi", "githubapi"} {
		roots := map[string]bool{}
		for _, path := range files {
			if strings.HasSuffix(path, "_test.go") {
				continue
			}
			f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
			if err != nil {
				t.Fatal(err)
			}
			ast.Inspect(f, func(n ast.Node) bool {
				if s, ok := n.(*ast.SelectorExpr); ok {
					if p, ok := s.X.(*ast.Ident); ok && p.Name == vendor {
						roots[s.Sel.Name] = true
					}
				}
				return true
			})
		}
		f, err := parser.ParseFile(token.NewFileSet(), vendor+"/"+map[string]string{"cfapi": "cloudflare", "githubapi": "github"}[vendor]+".gen.go", nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		types := map[string]*ast.TypeSpec{}
		for _, decl := range f.Decls {
			if g, ok := decl.(*ast.GenDecl); ok {
				for _, spec := range g.Specs {
					if s, ok := spec.(*ast.TypeSpec); ok {
						types[s.Name.Name] = s
					}
				}
			}
		}
		edges := map[string]map[string]bool{}
		add := func(name string, node ast.Node) {
			if edges[name] == nil {
				edges[name] = map[string]bool{}
			}
			ast.Inspect(node, func(n ast.Node) bool {
				if id, ok := n.(*ast.Ident); ok && types[id.Name] != nil {
					edges[name][id.Name] = true
				}
				return true
			})
		}
		for name, spec := range types {
			add(name, spec.Type)
		}
		for _, decl := range f.Decls {
			if fn, ok := decl.(*ast.FuncDecl); ok && fn.Recv != nil {
				ast.Inspect(fn.Recv, func(n ast.Node) bool {
					if id, ok := n.(*ast.Ident); ok && types[id.Name] != nil {
						add(id.Name, fn.Type)
					}
					return true
				})
			}
			if g, ok := decl.(*ast.GenDecl); ok && g.Tok == token.CONST {
				for _, s := range g.Specs {
					v := s.(*ast.ValueSpec)
					for _, n := range v.Names {
						if roots[n.Name] && v.Type != nil {
							ast.Inspect(v.Type, func(n ast.Node) bool {
								if id, ok := n.(*ast.Ident); ok {
									roots[id.Name] = true
								}
								return true
							})
						}
					}
				}
			}
		}
		// Request aliases are generated together with the underlying body type.
		for name, spec := range types {
			if _, alias := spec.Type.(*ast.Ident); alias {
				for target := range edges[name] {
					edges[target][name] = true
				}
			}
		}

		// oapi-codegen flattens allOf references into inline fields. Preserve
		// those schema edges when checking generated declaration reachability.
		vendorName := map[string]string{"cfapi": "cloudflare", "githubapi": "github"}[vendor]
		data, err := os.ReadFile("../api/vendor/" + vendorName + "/openapi.slice.json")
		if err != nil {
			t.Fatal(err)
		}
		var spec struct {
			Components struct {
				Schemas map[string]json.RawMessage `json:"schemas"`
			} `json:"components"`
		}
		if err := json.Unmarshal(data, &spec); err != nil {
			t.Fatal(err)
		}
		normalize := func(name string) string {
			return strings.ToLower(regexp.MustCompile(`[^a-zA-Z0-9]`).ReplaceAllString(name, ""))
		}
		names := map[string]string{}
		for name := range types {
			names[normalize(name)] = name
		}
		for schema, raw := range spec.Components.Schemas {
			source := names[normalize(schema)]
			if source == "" {
				continue
			}
			for _, match := range regexp.MustCompile(`"\$ref"\s*:\s*"#/components/schemas/([^"]+)"`).FindAllSubmatch(raw, -1) {
				if target := names[normalize(string(match[1]))]; target != "" {
					edges[source][target] = true
				}
			}
		}

		changed := true
		for changed {
			changed = false
			for name := range roots {
				for target := range edges[name] {
					if !roots[target] {
						roots[target] = true
						changed = true
					}
				}
			}
		}
		for name := range types {
			if !roots[name] {
				t.Errorf("%s.%s is not reachable from provider code", vendor, name)
			}
		}
	}
}
