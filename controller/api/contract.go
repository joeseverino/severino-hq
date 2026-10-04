// Package api carries the bridge contract into the binary. What the contract
// states and the generator does not emit (a pattern, a default) is read from
// it here, so HQ's declaration and the controller take the same text.
package api

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"sync"
)

//go:embed hq-controller.openapi.json
var contract []byte

var schemas = sync.OnceValues(func() (map[string]json.RawMessage, error) {
	var document struct {
		Components struct {
			Schemas map[string]json.RawMessage `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(contract, &document); err != nil {
		return nil, fmt.Errorf("parse the bridge contract: %w", err)
	}
	return document.Components.Schemas, nil
})

// lookup decodes the keyword at a path of keys under a component schema.
func lookup[T any](schema string, path []string) (T, error) {
	var value T
	name := strings.Join(append([]string{schema}, path...), ".")
	found, err := schemas()
	if err != nil {
		return value, err
	}
	node, ok := found[schema]
	for _, key := range path {
		if !ok {
			break
		}
		var object map[string]json.RawMessage
		if err := json.Unmarshal(node, &object); err != nil {
			return value, fmt.Errorf("the bridge contract's %s: %w", name, err)
		}
		node, ok = object[key]
	}
	if !ok {
		return value, fmt.Errorf("the bridge contract states no %s", name)
	}
	if err := json.Unmarshal(node, &value); err != nil {
		return value, fmt.Errorf("the bridge contract's %s: %w", name, err)
	}
	return value, nil
}

// Keyword is one string keyword of a component schema, by the path of keys
// under it. A keyword the contract does not state is an error, never "".
func Keyword(schema string, path ...string) (string, error) {
	value, err := lookup[string](schema, path)
	if err == nil && value == "" {
		err = fmt.Errorf("the bridge contract's %s is empty", strings.Join(append([]string{schema}, path...), "."))
	}
	return value, err
}

// Limit is one positive integer keyword, such as a maxLength.
func Limit(schema string, path ...string) (int, error) {
	value, err := lookup[int](schema, path)
	if err == nil && value <= 0 {
		err = fmt.Errorf("the bridge contract's %s is not a limit", strings.Join(append([]string{schema}, path...), "."))
	}
	return value, err
}

// must is for a package-level declaration: a controller built against a
// contract without the keyword does not start.
func must[T any](value T, err error) T {
	if err != nil {
		panic(err)
	}
	return value
}

// MustKeyword is Keyword, or a controller that does not start.
func MustKeyword(schema string, path ...string) string { return must(Keyword(schema, path...)) }

// MustLimit is Limit, or a controller that does not start.
func MustLimit(schema string, path ...string) int { return must(Limit(schema, path...)) }

// MustPattern compiles the pattern keyword at the path.
func MustPattern(schema string, path ...string) *regexp.Regexp {
	return regexp.MustCompile(MustKeyword(schema, append(path, "pattern")...))
}
