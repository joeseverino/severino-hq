package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"testing"
)

func declared(t *testing.T, source string) map[string]*ast.FuncDecl {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), "example.go", "package example\n"+source, 0)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]*ast.FuncDecl{}
	for _, d := range file.Decls {
		if f, ok := d.(*ast.FuncDecl); ok {
			out[f.Name.Name] = f
		}
	}
	return out
}

func TestDelegationNamesTheSameFileFunctionTheWholeBodyReturns(t *testing.T) {
	d := declared(t, `
func shared[T any](ref string, build func() T) T { return build() }
func plain(ref string) int { return shared(ref, func() int { return 1 }) }
func instanced(ref string) int { return shared[int](ref, func() int { return 1 }) }
`)
	for _, name := range []string{"plain", "instanced"} {
		if callee, ok := delegation(d[name], d); !ok || callee != "shared" {
			t.Errorf("%s: got %q %v", name, callee, ok)
		}
	}
}

func TestDelegationRefusesMoreThanOneCall(t *testing.T) {
	d := declared(t, `
type R struct{}
func (R) method() int { return 1 }
func shared(x int) int { return x }
func two(x int) int { x++; return shared(x) }
func outside(x int) int { return strconv.Itoa(x) }
func receiver(r R) int { return r.method() }
func param(shared func(int) int) int { return shared(1) }
func operator(x int) int { return shared(x) + 1 }
func spread(xs []int) int { return shared(xs...) }
`)
	for _, name := range []string{"two", "outside", "receiver", "param", "operator", "spread"} {
		if callee, ok := delegation(d[name], d); ok {
			t.Errorf("%s: read as a delegation to %q", name, callee)
		}
	}
}

func TestTableReadsTheCasesByTheTypesTheyName(t *testing.T) {
	d := declared(t, `
func a(err error) *E {
	var dns *net.DNSError
	reason := ""
	switch {
	case errors.As(err, &dns):
		reason = "no name"
	default:
		reason = fallback(err, "failed")
	}
	return &E{reason: reason}
}
func b(err error) *E {
	var lookup *net.DNSError
	reason := ""
	switch {
	case errors.As(err, &lookup):
		reason = "other words"
	}
	return &E{reason: reason}
}
`)
	x, ok := table(d["a"])
	y, ok2 := table(d["b"])
	if !ok || !ok2 || x.Cases[0] != y.Cases[0] || x.Return != y.Return {
		t.Fatalf("a renamed copy must read the same: %v %v", x, y)
	}
}

func TestTableRefusesLogicBesideTheSwitch(t *testing.T) {
	d := declared(t, `
func extra(err error) string {
	reason := ""
	log(err)
	switch {
	case err == nil:
		reason = "none"
	}
	return reason
}
func twoSets(err error) string {
	reason, other := "", ""
	switch {
	case err == nil:
		reason = "none"
		other = "x"
	}
	return reason + other
}
func mixed(err error) string {
	reason := ""
	other := ""
	switch {
	case err == nil:
		reason = "none"
	default:
		other = "x"
	}
	return reason + other
}
func computed(err error) string {
	reason := ""
	switch {
	case err == nil:
		reason = describe(explain(err))
	}
	return reason
}
func initialized(err error) string {
	var reason = compute()
	switch {
	case err == nil:
		reason = "none"
	}
	return reason
}
`)
	for _, name := range []string{"extra", "twoSets", "mixed", "computed", "initialized"} {
		if _, ok := table(d[name]); ok {
			t.Errorf("%s: read as a table", name)
		}
	}
}
