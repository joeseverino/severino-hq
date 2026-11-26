package secret

import (
	"bytes"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"fmt"
	"log/slog"
	"strings"
	"testing"
)

const held = `sentinel "held" token/0123+&`

// Every way Go prints or encodes a value writes none of a credential, held
// alone, by pointer, or inside a struct, slice or map, and the file writer's
// option writes it whole.
func TestAValueNeverPrintsAndOnlyItsWriterRevealsIt(t *testing.T) {
	value := New(held)
	type settings struct{ Token Value }
	operands := []any{value, &value, settings{value}, &settings{value}, []Value{value}, map[string]Value{"t": value}}
	var out bytes.Buffer
	for _, operand := range operands {
		for _, verb := range []string{"%v", "%+v", "%#v", "%s", "%q", "%x", "%d"} {
			fmt.Fprintf(&out, verb+"\n", operand)
		}
		fmt.Fprintln(&out, fmt.Errorf("refused: %v", operand))
		slog.New(slog.NewTextHandler(&out, nil)).Info("text", "operand", operand)
		slog.New(slog.NewJSONHandler(&out, nil)).Info("json", "operand", operand)
		encoded, err := json.Marshal(operand)
		if err != nil {
			t.Fatal(err)
		}
		out.Write(encoded)
	}
	text, _ := value.MarshalText()
	out.Write(text)
	if strings.Contains(out.String(), "sentinel") || strings.Contains(out.String(), "0123") {
		t.Fatalf("a value printed its credential:\n%s", out.String())
	}

	written, err := jsonv2.Marshal(settings{value}, Revealing)
	if err != nil {
		t.Fatal(err)
	}
	var read settings
	if err := json.Unmarshal(written, &read); err != nil || read.Token.Reveal() != held {
		t.Fatalf("the writer's encoding did not carry the credential back: %s (%v)", written, err)
	}
}
