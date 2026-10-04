// Package testclient makes a Connect client for a test's in-process server,
// which cannot listen below 1024. It asks for the test, so nothing but a test
// can call it.
package testclient

import (
	"testing"

	"github.com/joeseverino/severino-hq/controller/secrets/connect"
	"github.com/joeseverino/severino-hq/controller/secrets/connect/internal/client"
)

// New is connect.New without the privileged-port rule. Every other rule holds.
func New(t testing.TB, endpoint string, token connect.Token) (*connect.Client, error) {
	t.Helper()
	return client.New(endpoint, token, client.AnyPort)
}
