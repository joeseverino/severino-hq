package providers

import (
	"reflect"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
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

// The proxy host writer sends a request struct of its own; every field it
// names has to exist on the vendored spec's request body.
func TestNPMProxyHostRequestFieldsExistInTheVendoredSpec(t *testing.T) {
	known := jsonKeys(reflect.TypeFor[npmapi.CreateProxyHostJSONBody]())
	for key := range jsonKeys(reflect.TypeFor[npmProxyHostRequest]()) {
		if !known[key] {
			t.Errorf("the vendored NPM spec's proxy host request has no field %q", key)
		}
	}
}
