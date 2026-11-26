// Package secret is a credential's value as the controller holds it: a
// connection's secret setting, a token a handoff carries. Every way Go prints
// or encodes a value (fmt's verbs, a log record, text and JSON marshaling)
// writes Redacted for it, so a credential has no path into a message, a log
// line or an error by accident. The code that writes one to where it is used
// (a request header, an environment file, the document on disk) asks for it
// by name, through Reveal or Revealing.
package secret

import (
	"encoding/json/jsontext"
	jsonv2 "encoding/json/v2"
	"fmt"
	"io"
	"log/slog"
	"strings"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
)

// Value is one credential. Its zero value holds nothing.
type Value struct{ value string }

// New holds value as a credential.
func New(value string) Value { return Value{value: value} }

// Reveal is the credential itself, for the code that writes it where it is
// used.
func (v Value) Reveal() string { return v.value }

// IsZero reports whether the value holds nothing.
func (v Value) IsZero() bool { return v.value == "" }

// Holds reports whether check holds of the credential, which a check reads
// without the text leaving it.
func (v Value) Holds(check func(string) bool) bool { return check(v.value) }

// Trimmed is the credential without the space around it.
func (v Value) Trimmed() Value { return Value{value: strings.TrimSpace(v.value)} }

func (Value) String() string                 { return mask.Redacted }
func (Value) GoString() string               { return mask.Redacted }
func (Value) Format(state fmt.State, _ rune) { _, _ = io.WriteString(state, mask.Redacted) }
func (Value) LogValue() slog.Value           { return slog.StringValue(mask.Redacted) }
func (Value) MarshalText() ([]byte, error)   { return []byte(mask.Redacted), nil }
func (Value) MarshalJSON() ([]byte, error)   { return jsontext.AppendQuote(nil, mask.Redacted) }

// UnmarshalText reads a credential from the document or handoff that carries
// it.
func (v *Value) UnmarshalText(text []byte) error {
	v.value = string(text)
	return nil
}

// Revealing is the JSON option of the code that writes a file credentials are
// used from: each Value encodes as the credential, as Reveal gives it.
var Revealing = jsonv2.WithMarshalers(jsonv2.MarshalFunc(func(v Value) ([]byte, error) {
	return jsontext.AppendQuote(nil, v.Reveal())
}))
