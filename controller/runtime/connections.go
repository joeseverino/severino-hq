package runtime

import (
	"fmt"
	"os"
	"regexp"
	"sort"
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

func (e Environment) Required(prefix, name string) (string, error) {
	value := strings.TrimSpace(e[prefix+"_"+name])
	if value == "" {
		return "", &ProviderError{Message: "A setting this needs is not configured on the controller."}
	}
	return value, nil
}

func (e Environment) Prefixes() map[string]string {
	found := map[string]string{}
	for name, value := range e {
		if strings.HasSuffix(name, "_CONNECTION_REF") && value != "" {
			found[value] = strings.TrimSuffix(name, "_CONNECTION_REF")
		}
	}
	return found
}

func (e Environment) Provider(ref string) string {
	prefix := e.Prefixes()[ref]
	if prefix == "" {
		return ""
	}
	if declared := strings.TrimSpace(e[prefix+"_PROVIDER"]); declared != "" {
		return declared
	}
	return strings.ToLower(prefix)
}

func (e Environment) Prefix(provider, ref string) (string, error) {
	if ref != "" {
		if prefix := e.Prefixes()[ref]; prefix != "" {
			return prefix, nil
		}
		return "", &ProviderError{Message: fmt.Sprintf("No connection named %q was supplied to the controller.", ref)}
	}
	refs := e.Refs(provider)
	if len(refs) > 1 {
		return "", &ProviderError{Message: fmt.Sprintf("More than one connection is a %s; the resource has to say which.", provider)}
	}
	if len(refs) == 1 {
		return e.Prefixes()[refs[0]], nil
	}
	return strings.ToUpper(provider), nil
}

func (e Environment) Refs(provider string) []string {
	refs := []string{}
	for ref := range e.Prefixes() {
		if e.Provider(ref) == provider {
			refs = append(refs, ref)
		}
	}
	sort.Strings(refs)
	if len(refs) == 0 {
		if ref := strings.TrimSpace(e[strings.ToUpper(provider)+"_CONNECTION_REF"]); ref != "" {
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
	sort.Strings(refs)
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

// SSH is a connection's SSH endpoint, checked in the order and with the
// messages connection_env.ssh_target uses.
func (e Environment) SSH(ref string) (SSHTarget, error) {
	prefix := e.Prefixes()[ref]
	if prefix == "" || e[prefix+"_HOST"] == "" {
		return SSHTarget{}, &ProviderError{Message: fmt.Sprintf("Unknown certificate transport: %s.", ref)}
	}
	rawPort, err := e.Required(prefix, "PORT")
	if err != nil {
		return SSHTarget{}, err
	}
	port, err := strconv.Atoi(rawPort)
	if err != nil || strings.TrimLeft(rawPort, "0123456789") != "" || port < 1 || port > 65535 {
		return SSHTarget{}, &ProviderError{Message: fmt.Sprintf("The port configured for %s is not a port number.", ref)}
	}
	host, err := e.Required(prefix, "HOST")
	if err != nil {
		return SSHTarget{}, err
	}
	user, err := e.Required(prefix, "USER")
	if err != nil {
		return SSHTarget{}, err
	}
	// Both become ssh's destination argument, where a leading dash is an option.
	if !sshHost.MatchString(host) {
		return SSHTarget{}, &ProviderError{Message: fmt.Sprintf("The host configured for %s is not a host name or address.", ref)}
	}
	if !sshUser.MatchString(user) {
		return SSHTarget{}, &ProviderError{Message: fmt.Sprintf("The user configured for %s is not a login name.", ref)}
	}
	hostKey, err := e.Required(prefix, "HOST_KEY")
	if err != nil {
		return SSHTarget{}, err
	}
	return SSHTarget{Host: host, User: user, HostKey: hostKey, Port: port}, nil
}
