package providers

import (
	"math"
	"strconv"
	"strings"
)

// Python's view of a decoded JSON value, for providers whose Python reads are
// deliberately lenient field by field (str(x or ""), x == 1, int(x or 0)).

func (v *pyValue) UnmarshalJSON(data []byte) error {
	parsed, err := parsePy(data)
	if err != nil {
		return err
	}
	*v = parsed
	return nil
}

// MarshalJSON writes the value back; an absent value is null, as dict.get gives None.
func (v pyValue) MarshalJSON() ([]byte, error) {
	if !v.present() {
		return []byte("null"), nil
	}
	var out strings.Builder
	v.write(&out, 0, false, 0)
	return []byte(out.String()), nil
}

func (v pyValue) present() bool {
	return v.literal != "" || v.text != nil || v.array != nil || v.object
}

func pyText(s string) pyValue { return pyValue{text: &s} }

// or is Python's `v or fallback`.
func (v pyValue) or(fallback pyValue) pyValue {
	if v.truthy() {
		return v
	}
	return fallback
}

// get is dict.get(key) on an object; anything else has no keys.
func (v pyValue) get(key string) pyValue {
	if !v.object {
		return pyValue{}
	}
	if at := indexOf(v.keys, key); at >= 0 {
		return v.values[at]
	}
	return pyValue{}
}

// opt is the value as a pointer, nil when absent, for code that tests presence with nil.
func (v pyValue) opt() *pyValue {
	if !v.present() {
		return nil
	}
	return &v
}

// orText is Python's `x if x is present else default` for dict.get(key, default).
func (v pyValue) orText(fallback string) pyValue {
	if v.present() {
		return v
	}
	return pyText(fallback)
}

// eq is Python's == between two decoded values.
func (v pyValue) eq(other pyValue) bool { return pyEqual(v, other) }

func (v pyValue) eqText(s string) bool { return v.eq(pyText(s)) }

// isInt is isinstance(v, int): integers and bools.
func (v pyValue) isInt() bool {
	if v.literal == "true" || v.literal == "false" {
		return true
	}
	return v.literal != "" && v.literal != "null" && !strings.ContainsAny(v.literal, ".eE")
}

// toInt is Python's int() of the decoded value; false where int() raises.
func (v pyValue) toInt() (int64, bool) {
	switch {
	case v.literal == "true":
		return 1, true
	case v.literal == "false":
		return 0, true
	case v.text != nil:
		n, err := strconv.ParseInt(strings.TrimSpace(*v.text), 10, 64)
		return n, err == nil
	case v.isInt():
		n, err := strconv.ParseInt(v.literal, 10, 64)
		return n, err == nil
	case v.literal != "" && v.literal != "null":
		f, err := strconv.ParseFloat(v.literal, 64)
		if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
			return 0, false
		}
		return int64(math.Trunc(f)), true
	}
	return 0, false
}
