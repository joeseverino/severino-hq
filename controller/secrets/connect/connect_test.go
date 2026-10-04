package connect

import (
	"errors"
	"testing"
)

// The package's own constructor and check, which is all the renderer can
// reach, always carry the privileged-port rule.
func TestThePublicConstructorAlwaysRequiresAPrivilegedPort(t *testing.T) {
	token, err := NewToken([]byte("example.token\n"))
	if err != nil {
		t.Fatal(err)
	}
	for endpoint, want := range map[string]error{
		"http://127.0.0.1:1023":  nil,
		"http://127.0.0.1:880":   nil,
		"http://127.0.0.1:1024":  ErrPort,
		"http://127.0.0.1:8080":  ErrPort,
		"http://127.0.0.1:65535": ErrPort,
		"http://127.0.0.1:0":     ErrEndpoint,
		"http://192.0.2.1:880":   ErrEndpoint,
		"https://127.0.0.1:880":  ErrEndpoint,
	} {
		if err := CheckEndpoint(endpoint); !errors.Is(err, want) {
			t.Errorf("CheckEndpoint(%s) = %v, wanted %v", endpoint, err, want)
		}
		if _, err := New(endpoint, token); !errors.Is(err, want) {
			t.Errorf("New(%s) = %v, wanted %v", endpoint, err, want)
		}
	}
	if port, err := Port("http://127.0.0.1:880"); err != nil || port != 880 {
		t.Fatalf("Port: %d %v", port, err)
	}
}
