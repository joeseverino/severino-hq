// Package connections declares the document that carries provider connections
// from the root renderer to the controller: the renderer writes it, the
// controller reads it, and both use this one type.
package connections

import (
	"bytes"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strings"
	"syscall"
)

// SchemaVersion is the only document version either side accepts.
const SchemaVersion = 1

// MaxBytes bounds a document on read.
const MaxBytes = 1 << 20

// RefName is the variable every connection carries its own ref in.
const RefName = "CONNECTION_REF"

// Document is every connection the controller may open. It holds credentials:
// it is never logged, and its errors never carry a value.
type Document struct {
	SchemaVersion int          `json:"schema_version"`
	Connections   []Connection `json:"connections"`
}

// Connection is one connection's settings, named as the controller reads them:
// Values[NAME] answers the environment name PREFIX_NAME.
type Connection struct {
	Ref    string            `json:"ref"`
	Prefix string            `json:"prefix"`
	Values map[string]string `json:"values"`
}

var (
	refPattern  = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)
	namePattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
)

// ValidRef reports whether ref is a connection reference.
func ValidRef(ref string) bool { return refPattern.MatchString(ref) }

// ValidName reports whether name is a prefix or a variable name.
func ValidName(name string) bool { return namePattern.MatchString(name) }

// ErrInvalid is every reason a document is refused; match with errors.Is.
var ErrInvalid = errors.New("invalid connections document")

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
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
	refs, prefixes, names := map[string]bool{}, map[string]bool{}, map[string]bool{}
	for index, connection := range d.Connections {
		if !ValidRef(connection.Ref) {
			return invalid("connection %d has an invalid ref", index)
		}
		if !ValidName(connection.Prefix) {
			return invalid("connection %d has an invalid prefix", index)
		}
		if refs[connection.Ref] {
			return invalid("connection %d repeats a ref", index)
		}
		if prefixes[connection.Prefix] {
			return invalid("connection %d repeats a prefix", index)
		}
		refs[connection.Ref], prefixes[connection.Prefix] = true, true
		if connection.Values[RefName] != connection.Ref {
			return invalid("connection %d does not carry its own ref", index)
		}
		for name, value := range connection.Values {
			if !ValidName(name) {
				return invalid("connection %d has an invalid variable name", index)
			}
			if value == "" || strings.ContainsAny(value, "\x00\r\n") {
				return invalid("connection %d has an empty or multi-line value", index)
			}
			// PREFIX_NAME is one flat namespace: A + B_C and A_B + C would collide.
			flat := connection.Prefix + "_" + name
			if names[flat] {
				return invalid("connection %d collides with another connection's variable", index)
			}
			names[flat] = true
		}
	}
	return nil
}

// Encode is the document's one serialization: validated, connections sorted by
// ref, keys sorted, so the same connections always encode to the same bytes.
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

// Entries flattens the document to PREFIX_NAME settings.
func (d Document) Entries() map[string]string {
	entries := map[string]string{}
	for _, connection := range d.Connections {
		for name, value := range connection.Values {
			entries[connection.Prefix+"_"+name] = value
		}
	}
	return entries
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
