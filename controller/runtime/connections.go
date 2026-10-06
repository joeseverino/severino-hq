package runtime

import (
	"errors"
	"fmt"
	"maps"
	"os"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/connections"
)

// Environment is what the launcher tells a run that is not a connection: who
// the controller is, where the bridge is, and the files mounted for it. Each
// fact is a field read once from the variable its tag names, so nothing is
// looked up by a name spelled at the call. It holds no credential.
type Environment struct {
	// ID is the controller's name; ControllerID falls back to the machine's.
	ID string `env:"HQ_CONTROLLER_ID"`
	// BridgeSocket is HQ's bridge socket. There is no other way to reach HQ.
	BridgeSocket string `env:"SEVERINO_BRIDGE_SOCKET"`
	// ConnectionsFile is the connections document the launcher mounts.
	// Connections arrive in that document and never in the container's
	// environment, where `docker inspect` would show them.
	ConnectionsFile string `env:"HQ_CONTROLLER_CONNECTIONS"`
	// CAFile is the roots this host added to its own trust store.
	CAFile string `env:"HQ_CONTROLLER_CA_FILE"`
	// SSHDir holds the identities, signing keys and known hosts connections name.
	SSHDir string `env:"HQ_CONTROLLER_SSH_DIR"`
	// ACMEDir is the certificate lineage directory.
	ACMEDir                string `env:"HQ_ACME_DIR"`
	ACMEPropagationSeconds string `env:"ACME_PROPAGATION_SECONDS"`
	// Run is this run's nonce, which labels its own container.
	Run string `env:"HQ_CONTROLLER_RUN"`
	// Image is the image this run was started from, and SourceRepository the
	// repository that delivers it.
	Image            string `env:"HQ_CONTROLLER_IMAGE"`
	SourceRepository string `env:"SEVERINO_HQ_SOURCE_REPOSITORY"`
	// Files the launcher writes for the readings taken of this machine.
	TailnetStatus string `env:"SEVERINO_TAILNET_STATUS"`
	TailnetLock   string `env:"SEVERINO_TAILNET_LOCK"`
	HostFirewall  string `env:"SEVERINO_HOST_FIREWALL"`
	// RenderStatus names the secret renderers' status documents, as name=path
	// pairs separated by commas. The names are the launcher's own.
	RenderStatus string `env:"SEVERINO_RENDER_STATUS"`
	// HostUnits is what `systemctl show` printed for the shipped units.
	HostUnits string `env:"SEVERINO_HOST_UNITS"`
	// PortainerInspectConcurrency bounds container inspections in flight.
	PortainerInspectConcurrency string `env:"HQ_PORTAINER_INSPECT_CONCURRENCY"`
	// inherited is what a child process may inherit (childEnvironment).
	inherited map[string]string
}

// ControllerID is control_plane.providers.controller_id: HQ_CONTROLLER_ID, or
// the machine's own name.
func (e Environment) ControllerID() string {
	if e.ID != "" {
		return e.ID
	}
	name, _ := os.Hostname()
	return name
}

// environmentFields maps each variable an Environment reads to its field.
var environmentFields = func() map[string]int {
	fields := map[string]int{}
	environment := reflect.TypeFor[Environment]()
	for index := range environment.NumField() {
		if name := environment.Field(index).Tag.Get("env"); name != "" {
			fields[name] = index
		}
	}
	return fields
}()

// connectionMarker ends the name of the variable a connection once carried
// its ref in.
const connectionMarker = "_CONNECTION_REF"

// ReadEnvironment is the process environment as the facts a run reads, each
// with the space around it removed. A connection set in the environment is an
// error: connections arrive in the connections document and nowhere else.
func ReadEnvironment(entries []string) (Environment, error) {
	env := Environment{inherited: map[string]string{}}
	fields := reflect.ValueOf(&env).Elem()
	for _, entry := range entries {
		name, value, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		if strings.HasSuffix(name, connectionMarker) && value != "" {
			return Environment{}, errors.New("a connection is set in the environment; connections arrive in the connections document")
		}
		if index, known := environmentFields[name]; known {
			fields.Field(index).SetString(strings.TrimSpace(value))
		}
		if slices.Contains(childEnvironment, name) {
			env.inherited[name] = value
		}
	}
	return env, nil
}

// Connections is every connection a run was given, each a typed value held
// under its ref. It is never serialized or logged.
type Connections struct {
	byRef map[string]connections.Connection
	// whole holds each ref whose connection has every setting its shape requires.
	whole map[string]bool
}

// NewConnections holds each connection under its ref, with the space around
// its settings removed. A ref two connections share names neither, so which
// credential answers never depends on order.
func NewConnections(list ...connections.Connection) Connections {
	found := map[string]connections.Connection{}
	shared := map[string]bool{}
	for _, connection := range list {
		if _, seen := found[connection.Ref]; seen {
			shared[connection.Ref] = true
		}
		found[connection.Ref] = connection.Trimmed()
	}
	whole := map[string]bool{}
	for ref := range shared {
		delete(found, ref)
	}
	for ref, connection := range found {
		whole[ref] = connection.Complete()
	}
	return Connections{byRef: found, whole: whole}
}

// LoadConnections reads the connections document the environment names. The
// document must be this account's own private file. No document is no
// connection.
func LoadConnections(env Environment) (Connections, error) {
	if env.ConnectionsFile == "" {
		return NewConnections(), nil
	}
	document, err := connections.ReadFile(env.ConnectionsFile, os.Geteuid())
	if err != nil {
		return Connections{}, err
	}
	return NewConnections(document.Connections...), nil
}

// Why a connection could not be used; match with errors.Is.
var (
	// ErrSettingMissing never names the setting: a report must not carry
	// what a connection is made of.
	ErrSettingMissing      = errors.New("a setting this needs is not configured on the controller")
	ErrNoSuchConnection    = errors.New("not supplied to the controller")
	ErrForeignConnection   = errors.New("belongs to another provider")
	ErrAmbiguousConnection = errors.New("more than one connection answers; the resource has to name one")
	ErrBadSSHTarget        = errors.New("not a usable SSH destination")
)

// All is every connection, in ref order.
func (c Connections) All() []connections.Connection {
	found := make([]connections.Connection, 0, len(c.byRef))
	for _, ref := range slices.Sorted(maps.Keys(c.byRef)) {
		found = append(found, c.byRef[ref])
	}
	return found
}

// Get is the connection held under ref.
func (c Connections) Get(ref string) (connections.Connection, bool) {
	connection, ok := c.byRef[ref]
	return connection, ok
}

// Refs is every connection of a provider, in ref order.
func (c Connections) Refs(provider ConnectionProvider) []string {
	refs := []string{}
	for _, connection := range c.All() {
		if ConnectionProvider(connection.Provider) == provider {
			refs = append(refs, connection.Ref)
		}
	}
	return refs
}

// For is the provider's connection: the one ref names, or its only one. A
// named connection still has to be the provider's, or its credential goes to
// a vendor it was never issued for. One that lacks a setting its shape
// requires is not usable.
func (c Connections) For(provider ConnectionProvider, ref string) (connections.Connection, error) {
	if ref == "" {
		refs := c.Refs(provider)
		if len(refs) > 1 {
			return connections.Connection{}, &ProviderError{Message: string(provider) + " connections", Err: ErrAmbiguousConnection}
		}
		if len(refs) == 0 {
			return connections.Connection{}, &ProviderError{Err: ErrSettingMissing}
		}
		ref = refs[0]
	}
	connection, ok := c.byRef[ref]
	if !ok {
		return connections.Connection{}, &ProviderError{Message: "connection " + ref, Err: ErrNoSuchConnection}
	}
	if ConnectionProvider(connection.Provider) != provider {
		return connections.Connection{}, &ProviderError{Message: fmt.Sprintf("connection %s for %s", ref, provider), Err: ErrForeignConnection}
	}
	if !c.whole[ref] {
		return connections.Connection{}, &ProviderError{Err: ErrSettingMissing}
	}
	return connection, nil
}

// Need is a connection's settings in the shape a provider reads: member is
// the connection's member for that shape. A connection that arrived in
// another shape lacks every setting of this one.
func Need[S any](member *S) (S, error) {
	if member == nil {
		var none S
		return none, &ProviderError{Err: ErrSettingMissing}
	}
	return *member, nil
}

// Only is the settings of the one connection that arrived in a shape, for a
// shape no provider owns: pick returns a connection's member for it.
func Only[S any](c Connections, pick func(connections.Connection) *S) (S, error) {
	var found *S
	for _, connection := range c.All() {
		member := pick(connection)
		if member == nil || !c.whole[connection.Ref] {
			continue
		}
		if found != nil {
			var none S
			return none, &ProviderError{Err: ErrAmbiguousConnection}
		}
		found = member
	}
	return Need(found)
}

// SSHRefs is every connection that is an SSH transport, in ref order.
func (c Connections) SSHRefs() []string {
	refs := []string{}
	for _, connection := range c.All() {
		if transport := connection.SSHTransport; transport != nil && transport.Host != "" && transport.User != "" {
			refs = append(refs, connection.Ref)
		}
	}
	return refs
}

// RoleRefs is every SSH transport that declares a role, in ref order.
func (c Connections) RoleRefs(role string) []string {
	refs := []string{}
	for _, ref := range c.SSHRefs() {
		if c.byRef[ref].SSHTransport.Role == role {
			refs = append(refs, ref)
		}
	}
	return refs
}

// Manages is whether HQ may change things through a connection.
func (c Connections) Manages(ref string) bool { return c.byRef[ref].Manages }

var sshHost = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.:-]*$`)
var sshUser = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_.-]{0,31}$`)

type SSHTarget struct {
	Host, User, HostKey string
	Port                int
}

// SSH is a connection's SSH endpoint. Host and user become ssh's destination
// argument, so each is checked before it can be read as an option.
func (c Connections) SSH(ref string) (SSHTarget, error) {
	transport := c.byRef[ref].SSHTransport
	if transport == nil || transport.Host == "" {
		return SSHTarget{}, &ProviderError{Message: "connection " + ref + " has no SSH host", Err: ErrNoSuchConnection}
	}
	if transport.Port == "" || transport.User == "" || transport.HostKey == "" {
		return SSHTarget{}, &ProviderError{Err: ErrSettingMissing}
	}
	port, err := strconv.Atoi(transport.Port)
	if err != nil || strings.TrimLeft(transport.Port, "0123456789") != "" || port < 1 || port > 65535 {
		return SSHTarget{}, &ProviderError{Message: "port of " + ref, Err: ErrBadSSHTarget}
	}
	if !sshHost.MatchString(transport.Host) {
		return SSHTarget{}, &ProviderError{Message: "host of " + ref, Err: ErrBadSSHTarget}
	}
	if !sshUser.MatchString(transport.User) {
		return SSHTarget{}, &ProviderError{Message: "user of " + ref, Err: ErrBadSSHTarget}
	}
	return SSHTarget{Host: transport.Host, User: transport.User, HostKey: transport.HostKey, Port: port}, nil
}
