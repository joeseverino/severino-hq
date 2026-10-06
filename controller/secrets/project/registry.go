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

// Variables every projection states beside its shape's settings: the
// connection's own ref, the provider it is for, and whether HQ may change
// things through it.
const (
	RefVariable      = "CONNECTION_REF"
	ProviderVariable = "PROVIDER"
	ManagesVariable  = "MANAGES"
)

// Variables the renderer writes for every connection; a projection cannot define them.
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
	// Default is the value of an optional field the item does not carry.
	Default string `json:"default,omitzero"`
}

// Registry is hq/config/controller-connections.json: where on a vault item
// each setting of each connection shape comes from. HQ's registry emits it and
// the shapes themselves are the generated types of the connections document,
// so a registry that states another set of settings is refused. It names no
// connection; the vault is the inventory.
type Registry struct {
	SchemaVersion int                         `json:"schema_version"`
	Projections   map[string]map[string]Entry `json:"projections"`
}

// ErrRegistry is every reason a registry is refused.
var ErrRegistry = errors.New("invalid connection registry")

func registryError(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrRegistry, fmt.Sprintf(format, args...))
}

// ParseRegistry reads the registry strictly, and refuses one whose projections
// are not the shapes the connections document is typed with.
func ParseRegistry(data []byte) (Registry, error) {
	registry, err := parse(data)
	if err != nil {
		return Registry{}, err
	}
	if err := registry.matchesShapes(); err != nil {
		return Registry{}, err
	}
	return registry, nil
}

// parse reads a registry's own form strictly: every projection carries its
// ref, and every variable comes from exactly one place.
func parse(data []byte) (Registry, error) {
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
		if ref, ok := projection[RefVariable]; !ok || ref.Source != SourceRef {
			return Registry{}, registryError("projection %s does not carry %s", name, RefVariable)
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
			// A default stands in for a field an item may leave out.
			if entry.Default != "" && (entry.Source != SourceField || !entry.Optional) {
				valid = false
			}
			if !valid {
				return Registry{}, registryError("projection %s declares %s unusably", name, variable)
			}
		}
	}
	return registry, nil
}

// envelope reports a variable every projection may state beside its settings.
func envelope(variable string) bool {
	return variable == RefVariable || variable == ProviderVariable || variable == ManagesVariable
}

// matchesShapes refuses a registry whose projections are not the shapes the
// connections document is typed with: the same names, each with the same
// settings, required where the shape requires them.
func (r Registry) matchesShapes() error {
	shapes := connections.Shapes()
	if len(shapes) != len(r.Projections) {
		return registryError("the projections are not the shapes a connection arrives in")
	}
	for _, shape := range shapes {
		projection, declared := r.Projections[shape.Name]
		if !declared {
			return registryError("no projection for the %s shape", shape.Name)
		}
		settings := 0
		for variable := range projection {
			if !envelope(variable) {
				settings++
			}
		}
		if settings != len(shape.Settings) {
			return registryError("projection %s does not state its shape's settings", shape.Name)
		}
		for _, setting := range shape.Settings {
			if entry, stated := projection[setting.Name]; !stated || entry.Optional == setting.Required {
				return registryError("projection %s does not state its shape's settings", shape.Name)
			}
		}
	}
	return nil
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
