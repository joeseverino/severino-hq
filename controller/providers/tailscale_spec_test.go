package providers

import (
	"reflect"
	"strings"
	"testing"

	tsapi "tailscale.com/client/tailscale/v2"
)

// jsonKeys is the set of JSON field names a struct declares.
func jsonKeys(t reflect.Type) map[string]bool {
	keys := map[string]bool{}
	for field := range t.Fields() {
		if name, _, _ := strings.Cut(field.Tag.Get("json"), ","); name != "" && name != "-" {
			keys[name] = true
		}
	}
	return keys
}

// Devices, users and routes decode into the official client's types. Settings
// (a withheld one is null) and DNS (a resolver is a string or an object) use
// small local types; every field they read must exist on the official model,
// so a client upgrade that renames one fails here.
func TestTailnetFieldsExistInTheOfficialClient(t *testing.T) {
	reads := []struct {
		read, official reflect.Type
	}{
		{reflect.TypeFor[tailnetSettings](), reflect.TypeFor[tsapi.TailnetSettings]()},
		{reflect.TypeFor[tailnetDNSConfiguration](), reflect.TypeFor[tsapi.DNSConfiguration]()},
		{reflect.TypeFor[tailnetResolver](), reflect.TypeFor[tsapi.DNSConfigurationResolver]()},
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
