// Package connections declares the document that carries provider connections
// from the root renderer to the controller: the renderer writes it, the
// controller reads it, and both use the types generated from the schema HQ's
// registry emits (document.gen.go). A connection holds its settings as one
// typed value, so no setting is looked up by a name spelled at the call.
package connections

import (
	"bytes"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"syscall"
)

// SchemaVersion is the only document version either side accepts.
const SchemaVersion = 2

// MaxBytes bounds a document on read.
const MaxBytes = 1 << 20

var (
	refPattern  = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)
	namePattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
)

// ValidRef reports whether ref is a connection reference.
func ValidRef(ref string) bool { return refPattern.MatchString(ref) }

// ValidName reports whether name is a setting's name as a vault item's
// projection states it.
func ValidName(name string) bool { return namePattern.MatchString(name) }

// ErrInvalid is every reason a document is refused; match with errors.Is.
var ErrInvalid = errors.New("invalid connections document")

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}

// Setting is one setting of a shape as its type declares it.
type Setting struct {
	// Name is the setting's name in the registry: API_TOKEN.
	Name     string
	Required bool
	index    int
}

// Shape is one shape a connection can arrive in: the member of Connection
// that holds it, and the settings its type declares.
type Shape struct {
	// Name is the shape's name in the registry and its member name: api_token.
	Name     string
	Settings []Setting
	index    int
}

// shapes is every shape, read once from the generated Connection type: each
// member that points at a struct is one.
var shapes = func() []Shape {
	found := []Shape{}
	connection := reflect.TypeFor[Connection]()
	for index := range connection.NumField() {
		field := connection.Field(index)
		if field.Type.Kind() != reflect.Pointer || field.Type.Elem().Kind() != reflect.Struct {
			continue
		}
		name, _, _ := strings.Cut(field.Tag.Get("json"), ",")
		shape := Shape{Name: name, index: index}
		settings := field.Type.Elem()
		for at := range settings.NumField() {
			setting := settings.Field(at)
			shape.Settings = append(shape.Settings, Setting{
				Name: setting.Tag.Get("setting"), index: at,
				Required: !strings.Contains(setting.Tag.Get("json"), ",omitempty"),
			})
		}
		found = append(found, shape)
	}
	return found
}()

// Shapes is every shape a connection can arrive in.
func Shapes() []Shape { return append([]Shape{}, shapes...) }

// held is the shapes a connection holds settings under, with each one's settings.
func (c Connection) held() ([]Shape, []reflect.Value) {
	value := reflect.ValueOf(c)
	found, settings := []Shape{}, []reflect.Value{}
	for _, shape := range shapes {
		if member := value.Field(shape.index); !member.IsNil() {
			found, settings = append(found, shape), append(settings, member.Elem())
		}
	}
	return found, settings
}

// Shape is the name of the shape the connection arrived in: "" for a
// connection that holds none, or more than one.
func (c Connection) Shape() string {
	found, _ := c.held()
	if len(found) != 1 {
		return ""
	}
	return found[0].Name
}

// WithSettings is the connection holding settings under the named shape, by
// each setting's registry name. An unknown shape, or a name the shape does not
// declare, is an error.
func (c Connection) WithSettings(shape string, settings map[string]string) (Connection, error) {
	for _, declared := range shapes {
		if declared.Name != shape {
			continue
		}
		target := reflect.New(reflect.TypeFor[Connection]().Field(declared.index).Type.Elem())
		known := 0
		for _, setting := range declared.Settings {
			if value, given := settings[setting.Name]; given {
				target.Elem().Field(setting.index).SetString(value)
				known++
			}
		}
		if known != len(settings) {
			return Connection{}, invalid("a setting its shape does not declare")
		}
		reflect.ValueOf(&c).Elem().Field(declared.index).Set(target)
		return c, nil
	}
	return Connection{}, invalid("an unknown shape")
}

// Trimmed is the connection with the space around every setting removed.
func (c Connection) Trimmed() Connection {
	value := reflect.ValueOf(&c).Elem()
	for _, shape := range shapes {
		member := value.Field(shape.index)
		if member.IsNil() {
			continue
		}
		copied := reflect.New(member.Type().Elem())
		copied.Elem().Set(member.Elem())
		for _, setting := range shape.Settings {
			field := copied.Elem().Field(setting.index)
			field.SetString(strings.TrimSpace(field.String()))
		}
		member.Set(copied)
	}
	c.Provider = strings.TrimSpace(c.Provider)
	return c
}

// Complete reports whether the connection holds one shape and every setting
// that shape requires holds more than space.
func (c Connection) Complete() bool {
	found, settings := c.held()
	if len(found) != 1 {
		return false
	}
	for _, setting := range found[0].Settings {
		if setting.Required && strings.TrimSpace(settings[0].Field(setting.index).String()) == "" {
			return false
		}
	}
	return true
}

func singleLine(value string) bool { return !strings.ContainsAny(value, "\x00\r\n") }

// validate refuses one connection either side could misread.
func (c Connection) validate(index int) error {
	if !ValidRef(c.Ref) {
		return invalid("connection %d has an invalid ref", index)
	}
	if c.Provider == "" || !singleLine(c.Provider) {
		return invalid("connection %d has an empty or multi-line provider", index)
	}
	if c.Store.Vault == "" || c.Store.Item == "" || !singleLine(c.Store.Vault+c.Store.Item+c.Store.Bootstrap) {
		return invalid("connection %d has an empty or multi-line store", index)
	}
	found, settings := c.held()
	if len(found) != 1 {
		return invalid("connection %d does not hold exactly one shape", index)
	}
	for _, setting := range found[0].Settings {
		value := settings[0].Field(setting.index).String()
		if (value == "" && setting.Required) || !singleLine(value) {
			return invalid("connection %d has an empty or multi-line value", index)
		}
	}
	return nil
}

// Validate refuses a document either side could misread. Messages name a
// connection by position, never by value.
func (d Document) Validate() error {
	if d.SchemaVersion != SchemaVersion {
		return invalid("schema version %d is not %d", d.SchemaVersion, SchemaVersion)
	}
	if len(d.Connections) == 0 {
		return invalid("no connections")
	}
	refs := map[string]bool{}
	for index, connection := range d.Connections {
		if err := connection.validate(index); err != nil {
			return err
		}
		if refs[connection.Ref] {
			return invalid("connection %d repeats a ref", index)
		}
		refs[connection.Ref] = true
	}
	return nil
}

// Encode is the document's one serialization: validated, connections sorted by
// ref, so the same connections always encode to the same bytes.
func (d Document) Encode() ([]byte, error) {
	if err := d.Validate(); err != nil {
		return nil, err
	}
	sorted := Document{SchemaVersion: d.SchemaVersion, Connections: append([]Connection{}, d.Connections...)}
	sort.Slice(sorted.Connections, func(i, j int) bool { return sorted.Connections[i].Ref < sorted.Connections[j].Ref })
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(sorted); err != nil {
		return nil, invalid("not encodable")
	}
	// What Decode would refuse is never written.
	if out.Len() > MaxBytes {
		return nil, invalid("larger than %d bytes", MaxBytes)
	}
	return out.Bytes(), nil
}

// Decode reads one document strictly: an unknown or repeated field, a second
// value, a wrong version or an invalid connection is an error.
func Decode(data []byte) (Document, error) {
	if len(data) > MaxBytes {
		return Document{}, invalid("larger than %d bytes", MaxBytes)
	}
	var document Document
	// v2 refuses a repeated member name and trailing data on its own.
	if err := jsonv2.Unmarshal(data, &document, jsonv2.RejectUnknownMembers(true)); err != nil {
		// The decoder's message can quote the input, which is credentials.
		return Document{}, invalid("not the declared shape")
	}
	if err := document.Validate(); err != nil {
		return Document{}, err
	}
	return document, nil
}

// String names a connection without any of its settings, so one that reaches
// a log or an error by accident carries no credential.
func (c Connection) String() string {
	return fmt.Sprintf("connection %s (%s, %s)", c.Ref, c.Provider, c.Shape())
}

// Format writes String for every verb: %+v and %#v would otherwise print the
// settings.
func (c Connection) Format(state fmt.State, _ rune) { io.WriteString(state, c.String()) }

// LogValue is what a structured log records of a connection.
func (c Connection) LogValue() slog.Value {
	return slog.GroupValue(slog.String("ref", c.Ref), slog.String("provider", c.Provider), slog.String("shape", c.Shape()))
}

// ReadFile reads the document at path, which must be a regular file with one
// name, owned by uid and closed to every other account. The checks are made on
// the open file, so what is checked is what is read.
func ReadFile(path string, uid int) (Document, error) {
	listed, err := os.Lstat(path)
	if err != nil {
		return Document{}, invalid("not readable")
	}
	if !listed.Mode().IsRegular() {
		return Document{}, invalid("not a regular file")
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return Document{}, invalid("not readable")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !os.SameFile(listed, info) || !info.Mode().IsRegular() {
		return Document{}, invalid("not a regular file")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != uid || stat.Nlink != 1 {
		return Document{}, invalid("not a single-link file owned by uid %d", uid)
	}
	if info.Mode().Perm()&0o077 != 0 {
		return Document{}, invalid("readable or writable by another account")
	}
	data, err := io.ReadAll(io.LimitReader(file, MaxBytes+1))
	if err != nil {
		return Document{}, invalid("not readable")
	}
	return Decode(data)
}
