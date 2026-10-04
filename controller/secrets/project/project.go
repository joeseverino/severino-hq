package project

import (
	"bytes"
	"errors"
	"io/fs"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/secrets/connectapi"
)

// ErrContent is every refusal of what the vault holds; match with errors.Is.
// Messages name a connection by its validated ref, as the renderer always has,
// and never carry a field's value.
var ErrContent = errors.New("the vault's content was refused")

type contentError struct{ message string }

func (e *contentError) Error() string { return e.message }
func (e *contentError) Unwrap() error { return ErrContent }

func refuse(parts ...string) error { return &contentError{message: strings.Join(parts, "")} }

// Vault is the vault being rendered: how the host names it, and what Connect
// says its identifier and name are.
type Vault struct{ Configured, ID, Name string }

// Input is one consistent read of the vault and what to make of it.
type Input struct {
	Registry Registry
	Vault    Vault
	// EnvItem is the title or identifier of the application environment item.
	EnvItem string
	Items   []connectapi.FullItem
	// MinAppVariables is the fewest variables a real application environment has.
	MinAppVariables int
}

// File is one file of the controller's SSH directory.
type File struct {
	Name string
	Data []byte
	Mode fs.FileMode
	// Same reports whether an installed file already is this one. Nil compares bytes.
	Same func(installed []byte) bool
}

// Output is everything one render installs.
type Output struct {
	AppEnv       []byte
	AppVariables int
	Document     connections.Document
	// SSH holds known_hosts, each identity and its public half, and each
	// signing key and its public half, sorted by name.
	SSH         []File
	Identities  int
	SigningKeys int
}

func text(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func fields(item connectapi.FullItem) []connectapi.Field {
	if item.Fields == nil {
		return nil
	}
	return *item.Fields
}

// byLabel is one field's value by label, or empty. Two fields with the label
// are an error: which one answers must never depend on order.
func byLabel(item connectapi.FullItem, label string) (string, error) {
	found, value := 0, ""
	for _, field := range fields(item) {
		if text(field.Label) == label {
			found++
			value = text(field.Value)
		}
	}
	if found > 1 {
		return "", refuse("Duplicate connection metadata.")
	}
	return value, nil
}

// named is the items whose title or identifier is name.
func named(items []connectapi.FullItem, name string) []connectapi.FullItem {
	var found []connectapi.FullItem
	for _, item := range items {
		if text(item.Id) == name || text(item.Title) == name {
			found = append(found, item)
		}
	}
	return found
}

var appVariable = regexp.MustCompile(`^[A-Z][A-Z0-9_]+$`)
var emptyAssignment = regexp.MustCompile(`(?m)^[A-Za-z_][A-Za-z0-9_]*=$`)

// appEnvironment renders the environment item's UPPER_SNAKE fields as
// shell-quoted KEY='value' lines, the format hq/config/settings.py loads.
func appEnvironment(input Input) ([]byte, int, error) {
	matches := named(input.Items, input.EnvItem)
	if len(matches) != 1 {
		return nil, 0, refuse("The application environment item must resolve to exactly one item; found ", strconv.Itoa(len(matches)), ".")
	}
	var out bytes.Buffer
	count := 0
	seen := map[string]bool{}
	for _, field := range fields(matches[0]) {
		label, value := text(field.Label), text(field.Value)
		if !appVariable.MatchString(label) || value == "" {
			continue
		}
		if strings.ContainsAny(value, "\x00\r") {
			// The loader reads the file as text: a carriage return would not
			// come back as it was written.
			return nil, 0, refuse("Refusing: an application variable holds a NUL byte or a carriage return.")
		}
		if seen[label] {
			// Which one wins would depend on who reads the file.
			return nil, 0, refuse("Refusing: an application variable is set twice.")
		}
		seen[label] = true
		out.WriteString(label + "='" + strings.ReplaceAll(value, "'", `'\''`) + "'\n")
		count++
	}
	if out.Len() == 0 {
		return nil, 0, refuse("Refusing an empty render.")
	}
	if bytes.Contains(out.Bytes(), []byte("op://")) {
		return nil, 0, refuse("Refusing: an unresolved reference survived injection.")
	}
	if emptyAssignment.Match(out.Bytes()) {
		return nil, 0, refuse("Refusing: a variable rendered empty.")
	}
	if count < input.MinAppVariables {
		return nil, 0, refuse("Refusing suspiciously small app env (", strconv.Itoa(count), " vars) from 1Password.")
	}
	return out.Bytes(), count, nil
}

func hasControl(value string) bool {
	return strings.ContainsFunc(value, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

// connection projects one connection item, or returns nil for an item that is
// not a connection.
func connection(input Input, item connectapi.FullItem) (*connections.Connection, error) {
	ref, err := byLabel(item, "connection_ref")
	if err != nil {
		return nil, err
	}
	// Items without a connection_ref are not provider connections. The SSH key
	// items connections name are among them.
	if ref == "" {
		return nil, nil
	}
	// Every field, projected or not: a connection item holds single-line values.
	for _, field := range fields(item) {
		if strings.ContainsAny(text(field.Value), "\x00\r\n") {
			return nil, refuse("Invalid controller field value.")
		}
	}
	if item.Urls != nil {
		for _, url := range *item.Urls {
			if strings.ContainsAny(url.Href, "\x00\r\n") {
				return nil, refuse("Invalid controller field value.")
			}
		}
	}
	if !connections.ValidRef(ref) {
		return nil, refuse("Connection has an invalid connection_ref.")
	}
	name, err := byLabel(item, "projection")
	if err != nil {
		return nil, err
	}
	prefix, err := byLabel(item, "env_prefix")
	if err != nil {
		return nil, err
	}
	if name == "" || prefix == "" {
		return nil, refuse("Connection ", ref, " declares no projection or env_prefix.")
	}
	if !connections.ValidName(prefix) {
		return nil, refuse("Connection has an invalid env_prefix.")
	}
	projection, known := input.Registry.Projections[name]
	if !known {
		return nil, refuse("Connection ", ref, " names an unknown projection.")
	}
	values := map[string]string{}
	for _, variable := range variables(projection) {
		entry := projection[variable]
		value := ""
		switch entry.Source {
		case SourceRef:
			value = ref
		case SourceConstant:
			value = entry.Value
		case SourceURL:
			if item.Urls != nil && *entry.Index < len(*item.Urls) {
				value = (*item.Urls)[*entry.Index].Href
			}
		case SourceField:
			kind, selector := "label", entry.Label
			if entry.ID != "" {
				kind, selector = "id", entry.ID
			}
			found := 0
			for _, field := range fields(item) {
				if (kind == "id" && field.Id == selector) || (kind == "label" && text(field.Label) == selector) {
					found++
					value = text(field.Value)
				}
			}
			if found == 0 && entry.Optional {
				continue
			}
			if found != 1 {
				return nil, refuse("Connection ", ref, " must contain exactly one field ", kind, "=", selector, "; found ", strconv.Itoa(found), ".")
			}
		}
		if value == "" {
			if entry.Optional {
				continue
			}
			return nil, refuse("Connection ", ref, " is missing ", variable, ".")
		}
		if hasControl(value) {
			return nil, refuse("Connection ", ref, " has a control character in ", variable, ".")
		}
		if strings.Contains(value, "op://") {
			return nil, refuse("Refusing: an unresolved reference survived injection.")
		}
		values[variable] = value
	}
	// Where the credential is kept, so HQ can name the item a replacement is
	// stored into.
	values[StoreVault], values[StoreItem] = input.Vault.Configured, text(item.Id)
	if hasControl(input.Vault.Configured) || input.Vault.Configured == "" {
		return nil, refuse("The vault name is not a single line.")
	}
	bootstrap, err := byLabel(item, "bootstrap")
	if err != nil {
		return nil, err
	}
	if bootstrap != "" {
		if err := checkBootstrap(input.Vault, ref, bootstrap); err != nil {
			return nil, err
		}
		values[Bootstrap] = bootstrap
	}
	return &connections.Connection{Ref: ref, Prefix: prefix, Values: values}, nil
}

// checkBootstrap accepts op://<vault>/<item> in a vault the renderer does not
// read. One in the vault it reads is refused under every name that vault
// answers to: a reader of that vault could then mint.
func checkBootstrap(vault Vault, ref, bootstrap string) error {
	rest, ok := strings.CutPrefix(bootstrap, "op://")
	if !ok || hasControl(bootstrap) {
		return refuse("Connection ", ref, " has an invalid bootstrap reference.")
	}
	parts := strings.Split(rest, "/")
	// 1Password resolves a vault name without regard to case, and a stray
	// space is still the same vault to whoever reads the reference.
	inVault := strings.TrimSpace(parts[0])
	for _, own := range []string{vault.Configured, vault.ID, vault.Name} {
		if own = strings.TrimSpace(own); own != "" && strings.EqualFold(inVault, own) {
			return refuse("Connection ", ref, " keeps its bootstrap credential in the vault the controller reads.")
		}
	}
	switch {
	case len(parts) >= 3:
		return refuse("Connection ", ref, " names a bootstrap field; name the item: op://<vault>/<item>.")
	case len(parts) == 2 && parts[0] != "" && parts[1] != "":
		return nil
	}
	return refuse("Connection ", ref, " has an invalid bootstrap reference.")
}

// Project renders one read of the vault. Any refusal refuses the whole render.
func Project(input Input) (Output, error) {
	var out Output
	var err error
	if out.AppEnv, out.AppVariables, err = appEnvironment(input); err != nil {
		return Output{}, err
	}
	items := append([]connectapi.FullItem{}, input.Items...)
	sort.Slice(items, func(i, j int) bool { return text(items[i].Id) < text(items[j].Id) })
	refs, prefixes := map[string]bool{}, map[string]bool{}
	document := connections.Document{SchemaVersion: connections.SchemaVersion}
	for _, item := range items {
		found, err := connection(input, item)
		if err != nil {
			return Output{}, err
		}
		if found == nil {
			continue
		}
		if refs[found.Ref] {
			return Output{}, refuse("More than one 1Password item declares connection_ref=", found.Ref, ".")
		}
		if prefixes[found.Prefix] {
			return Output{}, refuse("More than one connection declares the same env_prefix.")
		}
		refs[found.Ref], prefixes[found.Prefix] = true, true
		document.Connections = append(document.Connections, *found)
	}
	// The renderer validates each connection; an empty inventory is also an error.
	if len(document.Connections) == 0 {
		return Output{}, refuse("Refusing a controller environment that resolved no connections.")
	}
	// Encoded here, with the size bound the controller reads under, so a
	// document it would refuse is a refusal of the vault's content and never
	// replaces the last good one.
	if _, err := document.Encode(); err != nil {
		return Output{}, refuse("Refusing a connections document the controller would not read.")
	}
	out.Document = document
	if out.SSH, out.Identities, out.SigningKeys, err = keys(input, document); err != nil {
		return Output{}, err
	}
	return out, nil
}
