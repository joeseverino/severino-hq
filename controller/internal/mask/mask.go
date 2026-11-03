// Package mask strikes every secret value this process has resolved from the
// text it writes: its log lines and what it reports to HQ. A value is learned
// once, where it enters the process (a connection's secret setting, a sealed
// value opened, a token minted or sent), the way a CI runner masks the values
// registered with it. Each learned value is then struck in every form it is
// commonly written in, so no call site has to word its message around one.
//
// The logger masks the bytes its handler writes rather than each attribute
// through slog.HandlerOptions.ReplaceAttr: a handler formats the attributes
// given to With when With is called (log/slog's commonHandler.withAttrs), so
// a value learned after that would pass, and ReplaceAttr never sees an
// attribute's key or a group's name. Each record is written in one Write.
package mask

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
)

// Redacted is what stands where a secret was.
const Redacted = "[redacted]"

// minimum is the shortest form struck. A shorter string is no credential, and
// striking it would strike it from every word that holds it.
const minimum = 4

var (
	mu      sync.Mutex
	learned = map[string]bool{}
	// replacer strikes every form of every learned value; nil until one is.
	replacer atomic.Pointer[strings.Replacer]
)

// Learn adds values to those struck from everything the process writes from
// now on. An empty or short value is ignored.
func Learn(values ...string) {
	mu.Lock()
	defer mu.Unlock()
	grew := false
	for _, value := range values {
		for _, form := range forms(value) {
			if !learned[form] {
				learned[form] = true
				grew = true
			}
		}
	}
	if !grew {
		return
	}
	// Longest first: where two forms start at one place, the longer is struck
	// whole (strings.NewReplacer tries its pairs in argument order).
	sorted := make([]string, 0, len(learned))
	for form := range learned {
		sorted = append(sorted, form)
	}
	slices.SortFunc(sorted, func(a, b string) int {
		if len(a) != len(b) {
			return len(b) - len(a)
		}
		return strings.Compare(a, b)
	})
	pairs := make([]string, 0, 2*len(sorted))
	for _, form := range sorted {
		pairs = append(pairs, form, Redacted)
	}
	replacer.Store(strings.NewReplacer(pairs...))
}

// Text is s with every learned value struck.
func Text(s string) string {
	if strike := replacer.Load(); strike != nil {
		return strike.Replace(s)
	}
	return s
}

// forms is a value as it is commonly written: plain, quoted the way Go and
// JSON quote it, URL-encoded, and base64-encoded, alone and as a run inside a
// longer encoding (the user:password of a Basic header).
func forms(value string) []string {
	if len(value) < minimum {
		return nil
	}
	found := []string{
		value,
		strings.TrimSuffix(strings.TrimPrefix(strconv.Quote(value), `"`), `"`),
		strings.TrimSuffix(strings.TrimPrefix(strconv.QuoteToASCII(value), `"`), `"`),
		url.QueryEscape(value),
		url.PathEscape(value),
	}
	if quoted, err := json.Marshal(value); err == nil {
		found = append(found, string(quoted[1:len(quoted)-1]))
	}
	var plain strings.Builder
	encoder := json.NewEncoder(&plain)
	encoder.SetEscapeHTML(false)
	if encoder.Encode(value) == nil {
		quoted := strings.TrimSpace(plain.String())
		found = append(found, quoted[1:len(quoted)-1])
	}
	for _, encoding := range []*base64.Encoding{base64.StdEncoding, base64.URLEncoding} {
		found = append(found, encoding.EncodeToString([]byte(value)))
		found = append(found, embedded(encoding.WithPadding(base64.NoPadding), value)...)
	}
	kept := []string{}
	for _, form := range found {
		if len(form) >= minimum && !slices.Contains(kept, form) {
			kept = append(kept, form)
		}
	}
	return kept
}

// embedded is the characters of value's base64 encoding that depend on value
// alone, for each of the three alignments it can start at inside a longer
// encoding.
func embedded(encoding *base64.Encoding, value string) []string {
	found := []string{}
	for offset := range 3 {
		encoded := encoding.EncodeToString(append(make([]byte, offset), value...))
		// Character i carries bits 6i to 6i+5; the value's are 8*offset up to
		// 8*(offset+len).
		start, end := (8*offset+5)/6, 8*(offset+len(value))/6
		if start < end {
			found = append(found, encoded[start:end])
		}
	}
	return found
}

// writer strikes every learned value from each write before passing it on.
type writer struct{ out io.Writer }

func (w writer) Write(p []byte) (int, error) {
	if _, err := io.WriteString(w.out, Text(string(p))); err != nil {
		return 0, err
	}
	return len(p), nil
}

// Writer is out with every learned value struck from what is written to it;
// out itself when it already is one.
func Writer(out io.Writer) io.Writer {
	if masked, ok := out.(writer); ok {
		return masked
	}
	return writer{out: out}
}

// Logger is the one logger a controller binary builds: slog's text handler
// writing to out through Writer.
func Logger(out io.Writer) *slog.Logger { return slog.New(slog.NewTextHandler(Writer(out), nil)) }
