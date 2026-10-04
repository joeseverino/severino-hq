package providers

import (
	"reflect"
	"strings"
	"testing"

	tsapi "tailscale.com/client/tailscale/v2"
)

// The provider reads Tailscale's API through its own tolerant structs (a
// withheld setting stays null, timestamps stay as written, a resolver is a
// string or an object), so the official client's strict types cannot decode
// for it. Every field it reads still has to exist on the official model, so a
// client upgrade that renames one fails here.
func TestTailnetFieldsExistInTheOfficialClient(t *testing.T) {
	reads := []struct {
		read, official reflect.Type
	}{
		{reflect.TypeOf(tailnetDevice{}), reflect.TypeOf(tsapi.Device{})},
		{reflect.TypeOf(tailnetDevice{}.ClientConnectivity), reflect.TypeOf(tsapi.ClientConnectivity{})},
		{reflect.TypeOf(tailnetSettings{}), reflect.TypeOf(tsapi.TailnetSettings{})},
		{reflect.TypeOf(tailnetUsers{}.Users).Elem(), reflect.TypeOf(tsapi.User{})},
		{reflect.TypeOf(tailnetDNSConfiguration{}), reflect.TypeOf(tsapi.DNSConfiguration{})},
	}
	for _, pair := range reads {
		known := map[string]bool{}
		for key := range jsonKeys(pair.official) {
			known[strings.ToLower(key)] = true
		}
		for key := range jsonKeys(pair.read) {
			if !known[strings.ToLower(key)] {
				t.Errorf("%s reads %q, which tailscale client v2 does not model", pair.read, key)
			}
		}
	}
}
