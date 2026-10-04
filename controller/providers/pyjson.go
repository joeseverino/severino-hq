package providers

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf16"
)

// pyValue is a JSON value with object key order kept, so it can be written
// back exactly as Python's json.dumps writes the same document.
type pyValue struct {
	literal string // null, true, false or a number, verbatim
	text    *string
	array   []pyValue
	keys    []string
	values  []pyValue
	object  bool
}

func parsePy(raw []byte) (pyValue, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := readPy(decoder)
	if err != nil {
		return pyValue{}, err
	}
	if _, err := decoder.Token(); err == nil {
		return pyValue{}, errors.New("trailing data")
	}
	return value, nil
}

func readPy(decoder *json.Decoder) (pyValue, error) {
	token, err := decoder.Token()
	if err != nil {
		return pyValue{}, err
	}
	switch t := token.(type) {
	case json.Delim:
		if t == '[' {
			out := pyValue{array: []pyValue{}}
			for decoder.More() {
				item, err := readPy(decoder)
				if err != nil {
					return pyValue{}, err
				}
				out.array = append(out.array, item)
			}
			_, err := decoder.Token()
			return out, err
		}
		out := pyValue{object: true}
		for decoder.More() {
			key, err := decoder.Token()
			if err != nil {
				return pyValue{}, err
			}
			item, err := readPy(decoder)
			if err != nil {
				return pyValue{}, err
			}
			// A repeated key keeps its first position and its last value, as a dict does.
			if at := indexOf(out.keys, key.(string)); at >= 0 {
				out.values[at] = item
				continue
			}
			out.keys = append(out.keys, key.(string))
			out.values = append(out.values, item)
		}
		_, err := decoder.Token()
		return out, err
	case string:
		return pyValue{text: &t}, nil
	case json.Number:
		return pyValue{literal: string(t)}, nil
	case bool:
		return pyValue{literal: strconv.FormatBool(t)}, nil
	default:
		return pyValue{literal: "null"}, nil
	}
}

func indexOf(values []string, want string) int {
	for i, value := range values {
		if value == want {
			return i
		}
	}
	return -1
}

// pyOptBool is a JSON field read as Python's bool(d.get(key, True)): absent is
// true, null is false, and any other value is its truthiness.
type pyOptBool struct {
	set   bool
	value pyValue
}

func (b *pyOptBool) UnmarshalJSON(data []byte) error {
	value, err := parsePy(data)
	if err != nil {
		return err
	}
	b.set, b.value = true, value
	return nil
}

func (b pyOptBool) orTrue() bool { return !b.set || b.value.truthy() }

// pyEqual is Python's == on two values json.loads produced: an int compares
// exactly, a float by its parsed value, a bool as 0 or 1, and an object ignores
// key order. So 1 == 1.0 and true == 1, as they are in a Python comparison.
func pyEqual(a, b pyValue) bool {
	if x, ok := a.number(); ok {
		y, ok := b.number()
		return ok && x.equal(y)
	}
	switch {
	case a.text != nil:
		return b.text != nil && *a.text == *b.text
	case a.object:
		if !b.object || len(a.keys) != len(b.keys) {
			return false
		}
		for i, key := range a.keys {
			at := indexOf(b.keys, key)
			if at < 0 || !pyEqual(a.values[i], b.values[at]) {
				return false
			}
		}
		return true
	case a.array != nil:
		if b.array == nil || len(a.array) != len(b.array) {
			return false
		}
		for i := range a.array {
			if !pyEqual(a.array[i], b.array[i]) {
				return false
			}
		}
		return true
	}
	return a.none() && b.none()
}

// none is Python's `is None` for a decoded value; an absent key reads as None.
func (v pyValue) none() bool { return !v.present() || v.literal == "null" }

// pyNum is a Python number: finite values exactly, an overflowed float as ±inf.
type pyNum struct {
	value *big.Rat
	inf   int
}

// less is x < y.
func (x pyNum) less(y pyNum) bool {
	if x.inf != 0 || y.inf != 0 {
		return x.inf < y.inf
	}
	return x.value.Cmp(y.value) < 0
}

func (x pyNum) equal(y pyNum) bool {
	if x.inf != 0 || y.inf != 0 {
		return x.inf == y.inf
	}
	return x.value.Cmp(y.value) == 0
}

func (v pyValue) number() (pyNum, bool) {
	if v.text != nil || v.object || v.array != nil {
		return pyNum{}, false
	}
	switch v.literal {
	case "true":
		return pyNum{value: big.NewRat(1, 1)}, true
	case "false":
		return pyNum{value: new(big.Rat)}, true
	case "null", "":
		return pyNum{}, false
	}
	if !strings.ContainsAny(v.literal, ".eE") {
		exact, ok := new(big.Rat).SetString(v.literal)
		return pyNum{value: exact}, ok
	}
	float, err := strconv.ParseFloat(v.literal, 64)
	if math.IsInf(float, 0) {
		if float > 0 {
			return pyNum{inf: 1}, true
		}
		return pyNum{inf: -1}, true
	}
	if err != nil {
		return pyNum{}, false
	}
	return pyNum{value: new(big.Rat).SetFloat64(float)}, true
}

// str is Python's str() of a decoded value: a string as itself, anything else
// as its repr.
func (v pyValue) str() string {
	if v.text != nil {
		return *v.text
	}
	return v.repr()
}

// repr is Python's repr() of a value json.loads produced.
func (v pyValue) repr() string {
	switch {
	case !v.present():
		return "None"
	case v.text != nil:
		return pyRepr(*v.text)
	case v.object:
		parts := make([]string, len(v.keys))
		for i, key := range v.keys {
			parts[i] = pyRepr(key) + ": " + v.values[i].repr()
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case v.array != nil:
		parts := make([]string, len(v.array))
		for i, item := range v.array {
			parts[i] = item.repr()
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	switch v.literal {
	case "null":
		return "None"
	case "true":
		return "True"
	case "false":
		return "False"
	}
	return pyNumber(v.literal)
}

// pyRepr is repr(str): single quotes unless the text holds one and no
// double quote, with Python's escapes for the quote, backslash and unprintables.
func pyRepr(text string) string {
	quote := '\''
	if strings.ContainsRune(text, '\'') && !strings.ContainsRune(text, '"') {
		quote = '"'
	}
	var out strings.Builder
	out.WriteRune(quote)
	for _, r := range text {
		switch {
		case r == quote || r == '\\':
			out.WriteRune('\\')
			out.WriteRune(r)
		case r == '\t':
			out.WriteString(`\t`)
		case r == '\n':
			out.WriteString(`\n`)
		case r == '\r':
			out.WriteString(`\r`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&out, `\x%02x`, r)
		case r > 0x7f && !unicode.IsPrint(r):
			switch {
			case r <= 0xff:
				fmt.Fprintf(&out, `\x%02x`, r)
			case r <= 0xffff:
				fmt.Fprintf(&out, `\u%04x`, r)
			default:
				fmt.Fprintf(&out, `\U%08x`, r)
			}
		default:
			out.WriteRune(r)
		}
	}
	out.WriteRune(quote)
	return out.String()
}

// strOr is str(d.get(key, fallback)).
func (v pyValue) strOr(key, fallback string) string {
	if field := v.get(key); field.present() {
		return field.str()
	}
	return fallback
}

// truthy is Python's bool() of the decoded value.
func (v pyValue) truthy() bool {
	switch {
	case !v.present():
		return false
	case v.text != nil:
		return *v.text != ""
	case v.object:
		return len(v.keys) > 0
	case v.array != nil:
		return len(v.array) > 0
	case v.literal == "null" || v.literal == "false":
		return false
	case v.literal == "true":
		return true
	}
	number, err := strconv.ParseFloat(v.literal, 64)
	return err != nil || number != 0
}

// pyDumps is json.dumps(value) with Python's defaults: ensure_ascii, ", " and
// ": " separators, or "," and ": " under an indent.
func pyDumps(raw []byte, indent int, sortKeys bool) (string, error) {
	value, err := parsePy(raw)
	if err != nil {
		return "", err
	}
	var out strings.Builder
	value.write(&out, indent, sortKeys, 0)
	return out.String(), nil
}

func (v pyValue) write(out *strings.Builder, indent int, sortKeys bool, depth int) {
	switch {
	case v.text != nil:
		writePyString(out, *v.text)
	case v.object:
		order := make([]int, len(v.keys))
		for i := range order {
			order[i] = i
		}
		if sortKeys {
			sort.SliceStable(order, func(a, b int) bool { return v.keys[order[a]] < v.keys[order[b]] })
		}
		writePyContainer(out, '{', '}', len(order), indent, depth, func(i int) {
			writePyString(out, v.keys[order[i]])
			out.WriteString(": ")
			v.values[order[i]].write(out, indent, sortKeys, depth+1)
		})
	case v.array != nil:
		writePyContainer(out, '[', ']', len(v.array), indent, depth, func(i int) {
			v.array[i].write(out, indent, sortKeys, depth+1)
		})
	default:
		out.WriteString(pyNumber(v.literal))
	}
}

func writePyContainer(out *strings.Builder, open, close byte, n, indent, depth int, item func(int)) {
	out.WriteByte(open)
	if n == 0 {
		out.WriteByte(close)
		return
	}
	for i := 0; i < n; i++ {
		if i > 0 {
			out.WriteByte(',')
			if indent == 0 {
				out.WriteByte(' ')
			}
		}
		if indent > 0 {
			out.WriteString("\n" + strings.Repeat(" ", indent*(depth+1)))
		}
		item(i)
	}
	if indent > 0 {
		out.WriteString("\n" + strings.Repeat(" ", indent*depth))
	}
	out.WriteByte(close)
}

// pyNumber is how Python writes a parsed number: integers verbatim, floats as repr.
func pyNumber(literal string) string {
	if !strings.ContainsAny(literal, ".eE") {
		return literal
	}
	f, err := strconv.ParseFloat(literal, 64)
	if err != nil {
		if math.IsInf(f, 0) {
			return map[bool]string{true: "Infinity", false: "-Infinity"}[f > 0]
		}
		return literal
	}
	exponent := strconv.FormatFloat(f, 'e', -1, 64)
	power, _ := strconv.Atoi(exponent[strings.IndexByte(exponent, 'e')+1:])
	if power < -4 || power >= 16 {
		return exponent
	}
	fixed := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(fixed, ".") {
		fixed += ".0"
	}
	return fixed
}

func writePyString(out *strings.Builder, s string) {
	out.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		default:
			if r >= 0x20 && r < 0x7f {
				out.WriteRune(r)
			} else if r > 0xffff {
				high, low := utf16.EncodeRune(r)
				fmt.Fprintf(out, `\u%04x\u%04x`, high, low)
			} else {
				fmt.Fprintf(out, `\u%04x`, r)
			}
		}
	}
	out.WriteByte('"')
}
