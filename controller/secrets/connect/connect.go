// Package connect reads a 1Password Connect server on this machine's IPv4
// loopback, on a port only root can listen on, and nowhere else.
package connect

import (
	"github.com/joeseverino/severino-hq/controller/secrets/connect/internal/client"
)

// The client's types, as the renderer names them.
type (
	Client = client.Client
	Token  = client.Token
	Error  = client.Error
	Health = client.Health
)

// Bounds on every exchange.
const (
	MaxResponseBytes = client.MaxResponseBytes
	MaxItems         = client.MaxItems
)

// Failure classes; match with errors.Is.
var (
	ErrEndpoint    = client.ErrEndpoint
	ErrPort        = client.ErrPort
	ErrNotLoopback = client.ErrNotLoopback
	ErrRedirect    = client.ErrRedirect
	ErrUnavailable = client.ErrUnavailable
	ErrDenied      = client.ErrDenied
	ErrResponse    = client.ErrResponse
	ErrIdentifier  = client.ErrIdentifier
)

// NewToken accepts the credential file's bytes: one line, in the JWT alphabet.
func NewToken(raw []byte) (Token, error) { return client.NewToken(raw) }

// ValidID reports whether id is a vault or item identifier.
func ValidID(id string) bool { return client.ValidID(id) }

// CheckEndpoint refuses any endpoint but http://127.0.0.1:PORT with PORT below
// 1024.
func CheckEndpoint(endpoint string) error { return client.CheckEndpoint(endpoint, client.Privileged) }

// Port is the port of an endpoint CheckEndpoint accepts.
func Port(endpoint string) (int, error) { return client.Port(endpoint) }

// New is the only way to make a client outside a test. There is no option,
// variable or flag that relaxes the port rule: it is applied here to the
// endpoint and again by the dialer to every connection.
func New(endpoint string, token Token) (*Client, error) {
	return client.New(endpoint, token, client.Privileged)
}
