package testrules

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// A subtest fails, logs and stops through its own t. One that reaches the t
// of a test around it (calling its Fatal or Error, handing it to a helper,
// or calling a closure of the parent's that holds it) fails the wrong test,
// and a FailNow on that t runs on the subtest's goroutine, which the testing
// package does not allow. govet's testinggoroutine, with its subtest flag
// (.golangci.yml), sees only a FailNow or Skip written in the subtest's own
// body.
func TestASubtestNamesOnlyItsOwnT(t *testing.T) {
	fset := token.NewFileSet()
	var found []string
	err := filepath.WalkDir("..", func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if name := entry.Name(); name == "testdata" || name == "vendor" || (strings.HasPrefix(name, ".") && name != "..") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, "_test.go") {
			return nil
		}
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return err
		}
		found = append(found, parentReaches(fset, file)...)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(found) > 0 {
		t.Errorf("a subtest reaches its parent's t; use the t its t.Run hands it:\n%s", strings.Join(found, "\n"))
	}
}

// The rule refuses each way a subtest reaches its parent's t, and passes a
// subtest that keeps to its own.
func TestTheSubtestRuleReadsEveryReach(t *testing.T) {
	refused := map[string]string{
		"a Fatal":                        `t.Run("x", func(_ *testing.T) { t.Fatal("x") })`,
		"an Error":                       `t.Run("x", func(_ *testing.T) { t.Error("x") })`,
		"a helper handed the parent's t": `t.Run("x", func(_ *testing.T) { check(t) })`,
		"another name for it":            `t.Run("x", func(_ *testing.T) { outer := t; outer.Fail() })`,
		"a closure of the parent's":      `fail := func() { t.Fatal("x") }; t.Run("x", func(_ *testing.T) { fail() })`,
		"a closure run as the subtest":   `body := func(_ *testing.T) { t.Fatal("x") }; t.Run("x", body)`,
		"a grandparent's t": `t.Run("x", func(sub *testing.T) {
			sub.Run("y", func(_ *testing.T) { t.Error("x") })
		})`,
		"a benchmark's b": `t.Run("x", func(_ *testing.T) { b.Fatal("x") })`,
	}
	for name, body := range refused {
		t.Run(name, func(t *testing.T) {
			if len(parentReaches(parsed(t, body))) == 0 {
				t.Errorf("not refused: %s", body)
			}
		})
	}
	kept := map[string]string{
		"its own t":                 `t.Run("x", func(t *testing.T) { t.Fatal("x") })`,
		"a helper handed its own t": `t.Run("x", func(sub *testing.T) { check(sub) })`,
		"a closure taking a t":      `fail := func(t *testing.T) { t.Fatal("x") }; t.Run("x", func(sub *testing.T) { fail(sub) })`,
		"a closure of its own": `t.Run("x", func(sub *testing.T) {
			fail := func() { sub.Fatal("x") }
			fail()
		})`,
		"a field named t":              `t.Run("x", func(sub *testing.T) { sub.Log(row.t, row{t: 1}) })`,
		"the parent after its subtest": `t.Run("x", func(sub *testing.T) { sub.Log() }); t.Fatal("x")`,
	}
	for name, body := range kept {
		t.Run(name, func(t *testing.T) {
			if found := parentReaches(parsed(t, body)); len(found) > 0 {
				t.Errorf("refused %s: %v", body, found)
			}
		})
	}
}

func parsed(t *testing.T, body string) (*token.FileSet, *ast.File) {
	t.Helper()
	fset := token.NewFileSet()
	source := "package x\n\nimport \"testing\"\n\nfunc TestX(t *testing.T, b *testing.B) {\n" + body + "\n}\n"
	file, err := parser.ParseFile(fset, "x_test.go", source, 0)
	if err != nil {
		t.Fatal(err)
	}
	return fset, file
}

// parentReaches is each place in file where a subtest's function reaches the
// t (or b) of a function around it, as "path:line: what it names".
func parentReaches(fset *token.FileSet, file *ast.File) []string {
	reported := map[token.Pos]string{}
	report := func(at ast.Node, message string) {
		reported[at.Pos()] = fset.Position(at.Pos()).String() + ": " + message
	}
	// closures holds, for each closure the file assigns a name, the testing
	// values of its enclosing functions it names.
	closures := map[string][]string{}
	var enclosing []ast.Node
	ast.Inspect(file, func(node ast.Node) bool {
		if node == nil {
			enclosing = enclosing[:len(enclosing)-1]
			return true
		}
		enclosing = append(enclosing, node)
		switch node := node.(type) {
		case *ast.AssignStmt:
			for i, value := range node.Rhs {
				if lit, ok := value.(*ast.FuncLit); ok && i < len(node.Lhs) {
					if name, ok := node.Lhs[i].(*ast.Ident); ok {
						closures[name.Name] = namedIn(lit, testingNames(enclosing))
					}
				}
			}
		case *ast.CallExpr:
			subtest, ok := subtestOf(node)
			if !ok {
				return true
			}
			outer := testingNames(enclosing)
			switch subtest := subtest.(type) {
			case *ast.FuncLit:
				outer = without(outer, testingParams(subtest.Type))
				uses(subtest.Body, outer, func(id *ast.Ident) {
					report(id, "names its parent's "+id.Name)
				})
				calls(subtest.Body, closures, outer, func(id *ast.Ident, held []string) {
					report(id, "calls "+id.Name+", which names its parent's "+strings.Join(held, ", "))
				})
			case *ast.Ident:
				if held := shared(closures[subtest.Name], outer); len(held) > 0 {
					report(subtest, "runs "+subtest.Name+", which names its parent's "+strings.Join(held, ", "))
				}
			}
		}
		return true
	})
	found := make([]string, 0, len(reported))
	for _, message := range reported {
		found = append(found, message)
	}
	slices.Sort(found)
	return found
}

// subtestOf is the function a call x.Run(name, fun) runs as a subtest.
func subtestOf(call *ast.CallExpr) (ast.Expr, bool) {
	selector, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || selector.Sel.Name != "Run" || len(call.Args) != 2 {
		return nil, false
	}
	switch fun := call.Args[1].(type) {
	case *ast.FuncLit:
		return fun, len(testingParams(fun.Type)) > 0
	case *ast.Ident:
		return fun, true
	}
	return nil, false
}

// testingNames is the testing values the functions in enclosing take, the
// innermost last.
func testingNames(enclosing []ast.Node) []string {
	var names []string
	for _, node := range enclosing {
		switch node := node.(type) {
		case *ast.FuncDecl:
			names = append(names, testingParams(node.Type)...)
		case *ast.FuncLit:
			names = append(names, testingParams(node.Type)...)
		}
	}
	return without(names, []string{"_"})
}

// testingParams is the names of a function's *testing.T, *testing.B and
// testing.TB parameters.
func testingParams(fun *ast.FuncType) []string {
	var names []string
	for _, field := range fun.Params.List {
		kind := field.Type
		if star, ok := kind.(*ast.StarExpr); ok {
			kind = star.X
		}
		selector, ok := kind.(*ast.SelectorExpr)
		if !ok {
			continue
		}
		if pkg, ok := selector.X.(*ast.Ident); !ok || pkg.Name != "testing" || !slices.Contains([]string{"T", "B", "TB"}, selector.Sel.Name) {
			continue
		}
		for _, name := range field.Names {
			names = append(names, name.Name)
		}
	}
	return names
}

// namedIn is which of names a closure's body names, beyond its own parameters.
func namedIn(lit *ast.FuncLit, names []string) []string {
	var held []string
	uses(lit.Body, without(names, paramNames(lit.Type)), func(id *ast.Ident) {
		if !slices.Contains(held, id.Name) {
			held = append(held, id.Name)
		}
	})
	return held
}

// uses calls found for each identifier under node that names one of names,
// where a function inside node that takes a parameter of that name hides it,
// and a selected field or a composite literal's key is not a use.
func uses(node ast.Node, names []string, found func(*ast.Ident)) {
	if len(names) == 0 {
		return
	}
	ast.Inspect(node, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.FuncLit:
			uses(n.Body, without(names, paramNames(n.Type)), found)
			return false
		case *ast.SelectorExpr:
			uses(n.X, names, found)
			return false
		case *ast.KeyValueExpr:
			if _, ok := n.Key.(*ast.Ident); ok {
				uses(n.Value, names, found)
				return false
			}
		case *ast.Ident:
			if slices.Contains(names, n.Name) {
				found(n)
			}
		}
		return true
	})
}

// calls calls found for each identifier under node that names a closure
// holding one of outer.
func calls(node ast.Node, closures map[string][]string, outer []string, found func(*ast.Ident, []string)) {
	ast.Inspect(node, func(n ast.Node) bool {
		if selector, ok := n.(*ast.SelectorExpr); ok {
			calls(selector.X, closures, outer, found)
			return false
		}
		if id, ok := n.(*ast.Ident); ok {
			if held := shared(closures[id.Name], outer); len(held) > 0 {
				found(id, held)
			}
		}
		return true
	})
}

func paramNames(fun *ast.FuncType) []string {
	var names []string
	for _, field := range fun.Params.List {
		for _, name := range field.Names {
			names = append(names, name.Name)
		}
	}
	return names
}

func without(names, hidden []string) []string {
	return slices.DeleteFunc(slices.Clone(names), func(name string) bool { return slices.Contains(hidden, name) })
}

func shared(held, outer []string) []string {
	return slices.DeleteFunc(slices.Clone(held), func(name string) bool { return !slices.Contains(outer, name) })
}
