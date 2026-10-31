// Command structural-inspector reads one Go file for the structural gate and prints what it declares as JSON; no provider code is executed.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"strings"
)

type Wrapper struct {
	Producer          string
	ProducerSignature string
	Signature         string
	Forward           string
}

// Table is a function that sets one variable in a switch and returns it.
type Table struct {
	Return string
	Cases  []string
}
type Result struct {
	Wrappers    map[string]Wrapper
	Delegations map[string]string
	Tables      map[string]Table
	Functions   map[string]bool
	Generated   bool
	Directives  []string
}

func show(n ast.Node) string {
	var b bytes.Buffer
	_ = printer.Fprint(&b, token.NewFileSet(), n)
	return b.String()
}
func ident(n ast.Node, s string) bool { v, ok := n.(*ast.Ident); return ok && v.Name == s }
func signature(t *ast.FuncType) string {
	clone := *t
	fields := func(list *ast.FieldList) *ast.FieldList {
		if list == nil {
			return nil
		}
		out := &ast.FieldList{}
		for _, field := range list.List {
			n := len(field.Names)
			if n == 0 {
				n = 1
			}
			for i := 0; i < n; i++ {
				out.List = append(out.List, &ast.Field{Type: field.Type})
			}
		}
		return out
	}
	clone.Params = fields(t.Params)
	clone.Results = fields(t.Results)
	return show(&clone)
}

func stringPrefix(n ast.Expr, constants map[string]bool) bool {
	if v, ok := n.(*ast.BasicLit); ok {
		return v.Kind == token.STRING
	}
	if v, ok := n.(*ast.Ident); ok {
		return constants[v.Name]
	}
	return false
}
func stringParameter(n ast.Expr, t *ast.FuncType) bool {
	name, ok := n.(*ast.Ident)
	if !ok {
		return false
	}
	for _, field := range t.Params.List {
		if ident(field.Type, "string") {
			for _, parameter := range field.Names {
				if parameter.Name == name.Name {
					return true
				}
			}
		}
	}
	return false
}
func decoration(f *ast.FuncDecl, constants map[string]bool) bool {
	if f == nil || f.Recv != nil || f.Body == nil || len(f.Body.List) != 1 || f.Type.Params == nil || len(f.Type.Params.List) != 1 || len(f.Type.Params.List[0].Names) != 1 || !ident(f.Type.Params.List[0].Type, "string") || f.Type.Results == nil || len(f.Type.Results.List) != 1 {
		return false
	}
	if constants[f.Type.Params.List[0].Names[0].Name] {
		return false
	}
	result, ok := f.Type.Results.List[0].Type.(*ast.MapType)
	if !ok || !ident(result.Key, "string") || !ident(result.Value, "string") {
		return false
	}
	ret, ok := f.Body.List[0].(*ast.ReturnStmt)
	if !ok || len(ret.Results) != 1 {
		return false
	}
	literal, ok := ret.Results[0].(*ast.CompositeLit)
	if !ok || show(literal.Type) != show(result) {
		return false
	}
	for _, element := range literal.Elts {
		field, ok := element.(*ast.KeyValueExpr)
		if !ok || !stringPrefix(field.Key, map[string]bool{}) {
			return false
		}
		if stringPrefix(field.Value, constants) || stringParameter(field.Value, f.Type) {
			continue
		}
		value, ok := field.Value.(*ast.BinaryExpr)
		if !ok || value.Op != token.ADD || !stringPrefix(value.X, constants) || !stringParameter(value.Y, f.Type) {
			return false
		}
	}
	return true
}

func wrapper(f *ast.FuncDecl, declarations map[string]*ast.FuncDecl, constants map[string]bool) (Wrapper, bool) {
	if f.Body == nil || len(f.Body.List) != 3 {
		return Wrapper{}, false
	}
	a, ok := f.Body.List[0].(*ast.AssignStmt)
	if !ok || a.Tok != token.DEFINE || len(a.Lhs) != 2 || len(a.Rhs) != 1 || !ident(a.Lhs[1], "err") {
		return Wrapper{}, false
	}
	credential, ok := a.Lhs[0].(*ast.Ident)
	if !ok {
		return Wrapper{}, false
	}
	call, ok := a.Rhs[0].(*ast.CallExpr)
	if !ok || call.Ellipsis.IsValid() {
		return Wrapper{}, false
	}
	for _, argument := range call.Args {
		if _, ok := argument.(*ast.Ident); !ok {
			return Wrapper{}, false
		}
	}
	producer := ""
	switch p := call.Fun.(type) {
	case *ast.Ident:
		producer = p.Name
	case *ast.SelectorExpr:
		if _, ok := p.X.(*ast.Ident); !ok {
			return Wrapper{}, false
		}
		producer = p.Sel.Name
	default:
		return Wrapper{}, false
	}
	declaration, ok := declarations[producer]
	if !ok || declaration == nil || declaration.Type.Results == nil || len(declaration.Type.Results.List) != 2 || !ident(declaration.Type.Results.List[0].Type, "string") || !ident(declaration.Type.Results.List[1].Type, "error") {
		return Wrapper{}, false
	}
	if selector, ok := call.Fun.(*ast.SelectorExpr); ok {
		if f.Recv == nil || declaration.Recv == nil || len(f.Recv.List) != 1 || len(declaration.Recv.List) != 1 || len(f.Recv.List[0].Names) != 1 || !ident(selector.X, f.Recv.List[0].Names[0].Name) || show(f.Recv.List[0].Type) != show(declaration.Recv.List[0].Type) {
			return Wrapper{}, false
		}
	} else {
		if declaration.Recv != nil {
			return Wrapper{}, false
		}
		for _, field := range f.Type.Params.List {
			for _, name := range field.Names {
				if name.Name == producer {
					return Wrapper{}, false
				}
			}
		}
	}
	guard, ok := f.Body.List[1].(*ast.IfStmt)
	if !ok || guard.Init != nil || guard.Else != nil || len(guard.Body.List) != 1 {
		return Wrapper{}, false
	}
	condition, ok := guard.Cond.(*ast.BinaryExpr)
	if !ok || condition.Op != token.NEQ || !ident(condition.X, "err") || !ident(condition.Y, "nil") {
		return Wrapper{}, false
	}
	failure, ok := guard.Body.List[0].(*ast.ReturnStmt)
	if !ok || len(failure.Results) != 2 || !ident(failure.Results[0], "nil") || !ident(failure.Results[1], "err") {
		return Wrapper{}, false
	}
	result, ok := f.Body.List[2].(*ast.ReturnStmt)
	if !ok || len(result.Results) != 1 {
		return Wrapper{}, false
	}
	forward, ok := result.Results[0].(*ast.CallExpr)
	if !ok || forward.Ellipsis.IsValid() {
		return Wrapper{}, false
	}
	valid := true
	used := false
	var expression func(ast.Expr) bool
	expression = func(n ast.Expr) bool {
		switch v := n.(type) {
		case *ast.Ident:
			if v.Name == credential.Name {
				used = true
				v.Name = "structuralCredential"
			}
			return true
		case *ast.BasicLit:
			return true
		case *ast.SelectorExpr:
			return expression(v.X)
		case *ast.BinaryExpr:
			return v.Op == token.ADD && stringPrefix(v.X, constants) && stringParameter(v.Y, f.Type) && expression(v.Y)
		case *ast.CompositeLit:
			for _, item := range v.Elts {
				switch e := item.(type) {
				case *ast.KeyValueExpr:
					if !expression(e.Key) || !expression(e.Value) {
						return false
					}
				case ast.Expr:
					if !expression(e) {
						return false
					}
				default:
					return false
				}
			}
			return true
		case *ast.CallExpr:
			name, ok := v.Fun.(*ast.Ident)
			if !ok || v.Ellipsis.IsValid() {
				return false
			}
			if name.Name == credential.Name {
				return false
			}
			for _, field := range f.Type.Params.List {
				for _, parameter := range field.Names {
					if parameter.Name == name.Name {
						return false
					}
				}
			}
			declaration := declarations[name.Name]
			if !decoration(declaration, constants) || len(v.Args) != 1 {
				return false
			}
			return expression(v.Args[0])
		default:
			return false
		}
	}
	if !expression(forward.Fun) {
		valid = false
	}
	for _, argument := range forward.Args {
		if !expression(argument) {
			valid = false
		}
	}
	if !valid || !used {
		return Wrapper{}, false
	}
	return Wrapper{show(call.Fun), signature(declaration.Type), signature(f.Type), show(forward)}, true
}

// delegation names the same-file function f's whole body returns a call of.
func delegation(f *ast.FuncDecl, declarations map[string]*ast.FuncDecl) (string, bool) {
	if f.Body == nil || len(f.Body.List) != 1 {
		return "", false
	}
	ret, ok := f.Body.List[0].(*ast.ReturnStmt)
	if !ok || len(ret.Results) != 1 {
		return "", false
	}
	call, ok := ret.Results[0].(*ast.CallExpr)
	if !ok || call.Ellipsis.IsValid() {
		return "", false
	}
	fun := call.Fun
	switch instance := fun.(type) {
	case *ast.IndexExpr:
		fun = instance.X
	case *ast.IndexListExpr:
		fun = instance.X
	}
	name, ok := fun.(*ast.Ident)
	if !ok || shadowed(f, name.Name) {
		return "", false
	}
	declaration := declarations[name.Name]
	if declaration == nil || declaration.Recv != nil || declaration == f {
		return "", false
	}
	return name.Name, true
}

func shadowed(f *ast.FuncDecl, name string) bool {
	lists := []*ast.FieldList{f.Recv, f.Type.Params, f.Type.Results}
	for _, list := range lists {
		if list == nil {
			continue
		}
		for _, field := range list.List {
			for _, parameter := range field.Names {
				if parameter.Name == name {
					return true
				}
			}
		}
	}
	return false
}

// table reads a function whose only logic is a switch that sets one variable
// from a literal or a call of identifiers and literals, then returns.
func table(f *ast.FuncDecl) (Table, bool) {
	if f.Body == nil || len(f.Body.List) < 2 {
		return Table{}, false
	}
	last, ok := f.Body.List[len(f.Body.List)-1].(*ast.ReturnStmt)
	if !ok {
		return Table{}, false
	}
	var choice *ast.SwitchStmt
	// A case names a declared variable by its type, so a renamed copy reads the same.
	types := map[string]string{}
	for _, statement := range f.Body.List[:len(f.Body.List)-1] {
		switch s := statement.(type) {
		case *ast.DeclStmt:
			d, ok := s.Decl.(*ast.GenDecl)
			if !ok || d.Tok != token.VAR {
				return Table{}, false
			}
			for _, spec := range d.Specs {
				v := spec.(*ast.ValueSpec)
				if len(v.Values) != 0 {
					return Table{}, false
				}
				for _, name := range v.Names {
					types[name.Name] = show(v.Type)
				}
			}
		case *ast.AssignStmt:
			if s.Tok != token.DEFINE || len(s.Lhs) != 1 || len(s.Rhs) != 1 {
				return Table{}, false
			}
			if _, ok := s.Rhs[0].(*ast.BasicLit); !ok {
				return Table{}, false
			}
		case *ast.SwitchStmt:
			if choice != nil || s.Init != nil {
				return Table{}, false
			}
			choice = s
		default:
			return Table{}, false
		}
	}
	if choice == nil {
		return Table{}, false
	}
	result := Table{Return: show(last)}
	target := ""
	for _, clause := range choice.Body.List {
		c := clause.(*ast.CaseClause)
		if len(c.Body) != 1 {
			return Table{}, false
		}
		set, ok := c.Body[0].(*ast.AssignStmt)
		if !ok || set.Tok != token.ASSIGN || len(set.Lhs) != 1 || len(set.Rhs) != 1 {
			return Table{}, false
		}
		variable, ok := set.Lhs[0].(*ast.Ident)
		if !ok || (target != "" && variable.Name != target) || !plain(set.Rhs[0]) {
			return Table{}, false
		}
		target = variable.Name
		for _, value := range c.List {
			ast.Inspect(value, func(n ast.Node) bool {
				if name, ok := n.(*ast.Ident); ok && types[name.Name] != "" {
					name.Name = "(" + types[name.Name] + ")"
				}
				return true
			})
			result.Cases = append(result.Cases, show(value))
		}
	}
	return result, true
}

// plain is a literal, or a call of a named function on identifiers and literals.
func plain(n ast.Expr) bool {
	switch v := n.(type) {
	case *ast.BasicLit:
		return true
	case *ast.CallExpr:
		if _, ok := v.Fun.(*ast.Ident); !ok || v.Ellipsis.IsValid() {
			return false
		}
		for _, argument := range v.Args {
			switch argument.(type) {
			case *ast.Ident, *ast.BasicLit:
			default:
				return false
			}
		}
		return true
	}
	return false
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "one source path required")
		os.Exit(1)
	}
	fs := token.NewFileSet()
	file, err := parser.ParseFile(fs, os.Args[1], nil, parser.ParseComments|parser.AllErrors)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	r := Result{Wrappers: map[string]Wrapper{}, Delegations: map[string]string{}, Tables: map[string]Table{}, Functions: map[string]bool{}}
	constants := map[string]bool{}
	for _, declaration := range file.Decls {
		if d, ok := declaration.(*ast.GenDecl); ok && d.Tok == token.CONST {
			for _, spec := range d.Specs {
				v := spec.(*ast.ValueSpec)
				for i, name := range v.Names {
					if i < len(v.Values) {
						if value, ok := v.Values[i].(*ast.BasicLit); ok && value.Kind == token.STRING {
							constants[name.Name] = true
						}
					}
				}
			}
		}
	}
	declarations := map[string]*ast.FuncDecl{}
	for _, d := range file.Decls {
		if f, ok := d.(*ast.FuncDecl); ok {
			r.Functions[f.Name.Name] = true
			if _, exists := declarations[f.Name.Name]; exists {
				declarations[f.Name.Name] = nil
				continue
			}
			declarations[f.Name.Name] = f
		}
	}
	for _, comments := range file.Comments {
		for _, c := range comments.List {
			if c.Pos() < file.Package && strings.HasPrefix(c.Text, "// Code generated ") && strings.HasSuffix(c.Text, " DO NOT EDIT.") {
				r.Generated = true
			}
			if after, ok := strings.CutPrefix(c.Text, "//go:generate "); ok {
				r.Directives = append(r.Directives, after)
			}
		}
	}
	for name, f := range declarations {
		if f == nil {
			continue
		}
		if w, ok := wrapper(f, declarations, constants); ok {
			r.Wrappers[name] = w
		}
		if callee, ok := delegation(f, declarations); ok {
			r.Delegations[name] = callee
		}
		if t, ok := table(f); ok {
			r.Tables[name] = t
		}
	}
	_ = json.NewEncoder(os.Stdout).Encode(r)
}
