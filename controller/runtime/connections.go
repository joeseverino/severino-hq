package runtime

import (
	"errors"
	"fmt"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// Environment is an immutable per-run copy. It is never serialized or logged.
type Environment map[string]string

// ControllerID is control_plane.providers.controller_id: HQ_CONTROLLER_ID, or
// the machine's own name.
func (e Environment) ControllerID() string {
	if id := strings.TrimSpace(e["HQ_CONTROLLER_ID"]); id != "" {
		return id
	}
	name, _ := os.Hostname()
	return name
}

func ParseEnvironment(entries []string) Environment {
	env := Environment{}
	for _, entry := range entries {
		name, value, ok := strings.Cut(entry, "=")
		if ok {
			env[name] = value
		}
	}
	return env
}

// Why a connection could not be used; match with errors.Is.
var (
	// ErrSettingMissing never names the setting: the name is the
	// connection's environment, which a report must not carry.
	ErrSettingMissing      = errors.New("a setting this needs is not configured on the controller")
	ErrNoSuchConnection    = errors.New("not supplied to the controller")
	ErrForeignConnection   = errors.New("belongs to another provider")
	ErrAmbiguousConnection = errors.New("more than one connection answers; the resource has to name one")
	ErrBadSSHTarget        = errors.New("not a usable SSH destination")
)

func (e Environment) Required(prefix, name string) (string, error) {
	value := strings.TrimSpace(e[prefix+"_"+name])
	if value == "" {
		return "", &ProviderError{Err: ErrSettingMissing}
	}
	return value, nil
}

// Prefixes maps each connection ref to its env prefix. A ref two prefixes
// share maps to neither, so which credential answers never depends on map order.
func (e Environment) Prefixes() map[string]string {
	found := map[string]string{}
	shared := map[string]bool{}
	for name, value := range e {
		if strings.HasSuffix(name, "_CONNECTION_REF") && value != "" {
			if _, seen := found[value]; seen {
				shared[value] = true
			}
			found[value] = strings.TrimSuffix(name, "_CONNECTION_REF")
		}
	}
	for ref := range shared {
		delete(found, ref)
	}
	return found
}

// Provider is the provider a connection declares, else its prefix. The value
// is the operator's, so it need not be one the contract names.
func (e Environment) Provider(ref string) ConnectionProvider {
	prefix := e.Prefixes()[ref]
	if prefix == "" {
		return ""
	}
	if declared := strings.TrimSpace(e[prefix+"_PROVIDER"]); declared != "" {
		return ConnectionProvider(declared)
	}
	return ConnectionProvider(strings.ToLower(prefix))
}

func (e Environment) Prefix(provider ConnectionProvider, ref string) (string, error) {
	if ref != "" {
		prefix := e.Prefixes()[ref]
		if prefix == "" {
			return "", &ProviderError{Message: "connection " + ref, Err: ErrNoSuchConnection}
		}
		// A named connection still has to be one of these, or its credential
		// goes to a vendor it was never issued for.
		if e.Provider(ref) != provider {
			return "", &ProviderError{Message: fmt.Sprintf("connection %s for %s", ref, provider), Err: ErrForeignConnection}
		}
		return prefix, nil
	}
	refs := e.Refs(provider)
	if len(refs) > 1 {
		return "", &ProviderError{Message: string(provider) + " connections", Err: ErrAmbiguousConnection}
	}
	if len(refs) == 1 {
		return e.Prefixes()[refs[0]], nil
	}
	return strings.ToUpper(string(provider)), nil
}

func (e Environment) Refs(provider ConnectionProvider) []string {
	refs := []string{}
	for ref := range e.Prefixes() {
		if e.Provider(ref) == provider {
			refs = append(refs, ref)
		}
	}
	slices.Sort(refs)
	if len(refs) == 0 {
		if ref := strings.TrimSpace(e[strings.ToUpper(string(provider))+"_CONNECTION_REF"]); ref != "" {
			refs = append(refs, ref)
		}
	}
	return refs
}

func (e Environment) SSHRefs() []string {
	refs := []string{}
	for ref, prefix := range e.Prefixes() {
		if e[prefix+"_HOST"] != "" && e[prefix+"_USER"] != "" {
			refs = append(refs, ref)
		}
	}
	slices.Sort(refs)
	return refs
}

func (e Environment) RoleRefs(role string) []string {
	refs := []string{}
	for _, ref := range e.SSHRefs() {
		if strings.TrimSpace(e[e.Prefixes()[ref]+"_ROLE"]) == role {
			refs = append(refs, ref)
		}
	}
	return refs
}

func (e Environment) Manages(ref string) bool {
	prefix := e.Prefixes()[ref]
	if prefix == "" {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(e[prefix+"_MANAGES"])) {
	case "1", "true", "yes":
		return true
	}
	return false
}

var sshHost = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.:-]*$`)
var sshUser = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_.-]{0,31}$`)

type SSHTarget struct {
	Host, User, HostKey string
	Port                int
}

// SSH is a connection's SSH endpoint. Host and user become ssh's destination
// argument, so each is checked before it can be read as an option.
func (e Environment) SSH(ref string) (SSHTarget, error) {
	prefix := e.Prefixes()[ref]
	if prefix == "" || e[prefix+"_HOST"] == "" {
		return SSHTarget{}, &ProviderError{Message: "connection " + ref + " has no SSH host", Err: ErrNoSuchConnection}
	}
	rawPort, err := e.Required(prefix, "PORT")
	if err != nil {
		return SSHTarget{}, err
	}
	port, err := strconv.Atoi(rawPort)
	if err != nil || strings.TrimLeft(rawPort, "0123456789") != "" || port < 1 || port > 65535 {
		return SSHTarget{}, &ProviderError{Message: "port of " + ref, Err: ErrBadSSHTarget}
	}
	host, err := e.Required(prefix, "HOST")
	if err != nil {
		return SSHTarget{}, err
	}
	user, err := e.Required(prefix, "USER")
	if err != nil {
		return SSHTarget{}, err
	}
	if !sshHost.MatchString(host) {
		return SSHTarget{}, &ProviderError{Message: "host of " + ref, Err: ErrBadSSHTarget}
	}
	if !sshUser.MatchString(user) {
		return SSHTarget{}, &ProviderError{Message: "user of " + ref, Err: ErrBadSSHTarget}
	}
	hostKey, err := e.Required(prefix, "HOST_KEY")
	if err != nil {
		return SSHTarget{}, err
	}
	return SSHTarget{Host: host, User: user, HostKey: hostKey, Port: port}, nil
}
