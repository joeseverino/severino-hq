package providers

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
)

// structKeys is the JSON field names of every struct type the files declare,
// an embedded struct's included.
func structKeys(t *testing.T, paths ...string) map[string][]string {
	t.Helper()
	types := map[string]*ast.StructType{}
	for _, path := range paths {
		f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if spec, ok := n.(*ast.TypeSpec); ok {
				if s, ok := spec.Type.(*ast.StructType); ok {
					types[spec.Name.Name] = s
				}
			}
			return true
		})
	}
	var keys func(s *ast.StructType) []string
	keys = func(s *ast.StructType) []string {
		found := []string{}
		for _, field := range s.Fields.List {
			if len(field.Names) == 0 {
				embedded := field.Type
				if star, ok := embedded.(*ast.StarExpr); ok {
					embedded = star.X
				}
				if ident, ok := embedded.(*ast.Ident); ok && types[ident.Name] != nil {
					found = append(found, keys(types[ident.Name])...)
				}
				continue
			}
			if field.Tag == nil {
				continue
			}
			tag, _ := strconv.Unquote(field.Tag.Value)
			if name, _, _ := strings.Cut(reflect.StructTag(tag).Get("json"), ","); name != "" && name != "-" {
				found = append(found, name)
			}
		}
		slices.Sort(found)
		return found
	}
	found := map[string][]string{}
	for name, s := range types {
		found[name] = keys(s)
	}
	return found
}

// Each record a reader sends is its kind's record in the bridge contract,
// which HQ's record model writes: a field one side names and the other does
// not is dropped on the way in, or never sent.
func TestReadingRecordsAreTheContractsRecords(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	files = slices.DeleteFunc(files, func(path string) bool { return strings.HasSuffix(path, "_test.go") })
	sent := structKeys(t, files...)
	contract := structKeys(t, "../runtime/bridge.gen.go")
	compared := 0
	for name, keys := range sent {
		want, twin := contract[name]
		if !twin || !strings.HasSuffix(name, "Record") {
			continue
		}
		compared++
		if !slices.Equal(keys, want) {
			t.Errorf("%s sends %v\nthe contract's record is %v", name, keys, want)
		}
	}
	if compared == 0 {
		t.Fatal("no reader record has a twin in the contract")
	}
}
