package connections

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"log/slog"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
)

// secretTypes is every type declared in the generated document that has a
// field tagged secret, read from the file itself so a type the registry adds
// is held without being named here.
func secretTypes(t *testing.T) []string {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), "document.gen.go", nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatal(err)
	}
	found := []string{}
	for _, declaration := range file.Decls {
		general, ok := declaration.(*ast.GenDecl)
		if !ok {
			continue
		}
		for _, spec := range general.Specs {
			typed, ok := spec.(*ast.TypeSpec)
			if !ok {
				continue
			}
			structure, ok := typed.Type.(*ast.StructType)
			if !ok {
				continue
			}
			for _, field := range structure.Fields.List {
				if field.Tag == nil {
					continue
				}
				tag, err := strconv.Unquote(field.Tag.Value)
				if err != nil {
					t.Fatal(err)
				}
				if reflect.StructTag(tag).Get(secretTag) == "true" {
					found = append(found, typed.Name.Name)
					break
				}
			}
		}
	}
	return found
}

// shapeTypes is each settings type a Connection holds, by name.
func shapeTypes() map[string]reflect.Type {
	found := map[string]reflect.Type{}
	connection := reflect.TypeFor[Connection]()
	for field := range connection.Fields() {
		if field := field.Type; field.Kind() == reflect.Pointer && field.Elem().Kind() == reflect.Struct {
			found[field.Elem().Name()] = field.Elem()
		}
	}
	return found
}

// filled is a value of kind with every string field holding its own
// sentinel, and the sentinels of the secret ones.
func filled(kind reflect.Type) (reflect.Value, []string) {
	value := reflect.New(kind).Elem()
	secrets := []string{}
	for index := range kind.NumField() {
		field := kind.Field(index)
		if field.Type.Kind() != reflect.String {
			continue
		}
		sentinel := "sentinel-" + kind.Name() + "-" + field.Name + "-value"
		value.Field(index).SetString(sentinel)
		if isSecret(field) {
			secrets = append(secrets, sentinel)
		}
	}
	return value, secrets
}

func TestNoSecretSettingPrintsOrLogs(t *testing.T) {
	names := secretTypes(t)
	if len(names) == 0 {
		t.Fatal("document.gen.go declares no type with a secret setting; the check reads nothing")
	}
	shapes := shapeTypes()
	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			kind, ok := shapes[name]
			if !ok {
				t.Fatalf("%s carries a secret but is no shape of Connection, so nothing here can build one", name)
			}
			value, secrets := filled(kind)
			pointer := reflect.New(kind)
			pointer.Elem().Set(value)
			wrapped := struct{ Settings any }{value.Interface()}
			outputs := map[string]string{}
			for _, operand := range []struct {
				name  string
				value any
			}{{"value", value.Interface()}, {"pointer", pointer.Interface()}, {"field", wrapped}} {
				for _, verb := range []string{"%v", "%+v", "%#v", "%s"} {
					outputs[operand.name+" "+verb] = fmt.Sprintf(verb, operand.value)
				}
				outputs[operand.name+" error"] = fmt.Errorf("failed with %v", operand.value).Error()
				for handler, build := range map[string]func(*bytes.Buffer) slog.Handler{
					"text": func(out *bytes.Buffer) slog.Handler { return slog.NewTextHandler(out, nil) },
					"json": func(out *bytes.Buffer) slog.Handler { return slog.NewJSONHandler(out, nil) },
				} {
					// The JSON handler encodes a struct it holds as the document
					// does, which has to carry the secret; it reads LogValue only
					// of the operand it is given.
					if handler == "json" && operand.name == "field" {
						continue
					}
					var out bytes.Buffer
					slog.New(build(&out)).Info("settings", slog.Any("settings", operand.value))
					outputs[operand.name+" slog "+handler] = out.String()
				}
			}
			for form, output := range outputs {
				for _, secret := range secrets {
					if strings.Contains(output, secret) {
						t.Errorf("%s %s carries its secret: %s", name, form, output)
					}
				}
			}
		})
	}
}

func TestSecretsAreTheTaggedSettings(t *testing.T) {
	shapes := shapeTypes()
	for _, name := range secretTypes(t) {
		value, want := filled(shapes[name])
		if got := secretsOf(value); !slices.Equal(got, want) {
			t.Errorf("%s: secretsOf is %v, want %v", name, got, want)
		}
	}
	connection := Connection{Ref: "example", Login: &Login{URL: "https://a.example.com", Username: "reader", Password: secret}}
	if got := connection.Secrets(); !slices.Equal(got, []string{secret}) {
		t.Errorf("a login connection's secrets are %v", got)
	}
}
