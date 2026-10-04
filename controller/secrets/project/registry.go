// Package project turns vault items into what the host installs: the
// application environment, the controller's connections document, and the SSH
// identities and signing keys connections name. It is pure: it reads no file
// and opens no socket, so every refusal in it can be tested and fuzzed.
package project

import (
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"sort"

	"github.com/joeseverino/severino-hq/controller/connections"
)

// Sources a projected variable can come from.
const (
	SourceRef      = "connection_ref"
	SourceField    = "field"
	SourceURL      = "url"
	SourceConstant = "constant"
)

// Variables the renderer adds to every connection; a projection cannot define them.
const (
	StoreVault = "STORE_VAULT"
	StoreItem  = "STORE_ITEM"
	Bootstrap  = "BOOTSTRAP"
)

// Entry is where one variable of a projection comes from.
type Entry struct {
	Source   string `json:"source"`
	ID       string `json:"id,omitzero"`
	Label    string `json:"label,omitzero"`
	Value    string `json:"value,omitzero"`
	Index    *int   `json:"index,omitzero"`
	Optional bool   `json:"optional,omitzero"`
}

// Registry is hq/config/controller-connections.json: the shapes a connection
// can take. It names no connection; the vault is the inventory.
type Registry struct {
	SchemaVersion int                         `json:"schema_version"`
	Projections   map[string]map[string]Entry `json:"projections"`
}

// ErrRegistry is every reason a registry is refused.
var ErrRegistry = errors.New("invalid connection registry")

func registryError(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrRegistry, fmt.Sprintf(format, args...))
}

// ParseRegistry reads the registry strictly.
func ParseRegistry(data []byte) (Registry, error) {
	var registry Registry
	if err := jsonv2.Unmarshal(data, &registry, jsonv2.RejectUnknownMembers(true)); err != nil {
		return Registry{}, registryError("not the declared shape")
	}
	if registry.SchemaVersion != 1 {
		return Registry{}, registryError("schema version %d is not 1", registry.SchemaVersion)
	}
	if len(registry.Projections) == 0 {
		return Registry{}, registryError("no projections")
	}
	for name, projection := range registry.Projections {
		if name == "" {
			return Registry{}, registryError("a projection has no name")
		}
		if ref, ok := projection[connections.RefName]; !ok || ref.Source != SourceRef {
			return Registry{}, registryError("projection %s does not carry %s", name, connections.RefName)
		}
		for variable, entry := range projection {
			if !connections.ValidName(variable) {
				return Registry{}, registryError("projection %s has an invalid variable name", name)
			}
			if variable == StoreVault || variable == StoreItem || variable == Bootstrap {
				return Registry{}, registryError("projection %s defines %s, which the renderer writes", name, variable)
			}
			selectors := 0
			for _, selector := range []string{entry.ID, entry.Label} {
				if selector != "" {
					selectors++
				}
			}
			valid := false
			switch entry.Source {
			case SourceRef:
				valid = selectors == 0 && entry.Value == "" && entry.Index == nil
			case SourceField:
				valid = selectors == 1 && entry.Value == "" && entry.Index == nil
			case SourceURL:
				valid = selectors == 0 && entry.Value == "" && entry.Index != nil && *entry.Index >= 0
			case SourceConstant:
				valid = selectors == 0 && entry.Value != "" && entry.Index == nil
			}
			if !valid {
				return Registry{}, registryError("projection %s declares %s unusably", name, variable)
			}
		}
	}
	return registry, nil
}

// variables is a projection's variable names, sorted: the order they render in.
func variables(projection map[string]Entry) []string {
	names := make([]string, 0, len(projection))
	for name := range projection {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
