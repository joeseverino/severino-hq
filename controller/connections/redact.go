package connections

import (
	"log/slog"
	"reflect"
	"strings"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
)

// secretTag marks a setting the registry declares secret, on the field the
// generator emits for it.
const secretTag = "secret"

func isSecret(field reflect.StructField) bool { return field.Tag.Get(secretTag) == "true" }

// shown is one setting as it may be printed: a secret one that is set is
// Redacted.
func shown(field reflect.StructField, value reflect.Value) string {
	if isSecret(field) && value.String() != "" {
		return mask.Redacted
	}
	return value.String()
}

// redactedString is a shape's settings, each by its field name, as %+v would
// print them with every secret one redacted.
func redactedString(settings any) string {
	value := reflect.ValueOf(settings)
	kind := value.Type()
	var out strings.Builder
	out.WriteString(kind.Name() + "{")
	for index := range kind.NumField() {
		if index > 0 {
			out.WriteByte(' ')
		}
		field := kind.Field(index)
		out.WriteString(field.Name + ":" + shown(field, value.Field(index)))
	}
	out.WriteByte('}')
	return out.String()
}

// redactedLog is a shape's settings as a structured log records them, each
// under its registry name, with every secret one redacted.
func redactedLog(settings any) slog.Value {
	value := reflect.ValueOf(settings)
	kind := value.Type()
	attrs := make([]slog.Attr, 0, kind.NumField())
	for index := range kind.NumField() {
		field := kind.Field(index)
		attrs = append(attrs, slog.String(field.Tag.Get("setting"), shown(field, value.Field(index))))
	}
	return slog.GroupValue(attrs...)
}

// secretsOf is the value of each setting the registry declares secret in one
// shape's settings.
func secretsOf(settings reflect.Value) []string {
	found := []string{}
	for index := range settings.NumField() {
		if isSecret(settings.Type().Field(index)) {
			found = append(found, settings.Field(index).String())
		}
	}
	return found
}

// Secrets is the value of each secret setting the connection holds, which
// the controller learns (mask.Learn) as it is handed the connection.
func (c Connection) Secrets() []string {
	_, settings := c.held()
	found := []string{}
	for _, held := range settings {
		found = append(found, secretsOf(held)...)
	}
	return found
}
