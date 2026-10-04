package providers

import (
	"reflect"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
)

type keySet map[string]bool

func (k keySet) list() []string {
	out := []string{}
	for key := range k {
		out = append(out, key)
	}
	return out
}

// jsonKeys is the set of JSON field names a struct declares.
func jsonKeys(t reflect.Type) keySet {
	keys := keySet{}
	for i := 0; i < t.NumField(); i++ {
		if name, _, _ := strings.Cut(t.Field(i).Tag.Get("json"), ","); name != "" && name != "-" {
			keys[name] = true
		}
	}
	return keys
}

// The npm readers pick fields out of NPM's records by name; every name has to
// exist on the vendored spec's model of that record.
func TestNPMRecordFieldsExistInTheVendoredSpec(t *testing.T) {
	reads := []struct {
		model reflect.Type
		keys  []string
	}{
		{reflect.TypeOf(npmapi.ProxyHostObject{}), []string{"id", "domain_names", "forward_scheme", "forward_host", "forward_port", "ssl_forced", "http2_support", "allow_websocket_upgrade", "caching_enabled", "block_exploits", "access_list_id", "advanced_config", "hsts_enabled", "hsts_subdomains", "trust_forwarded_proto", "enabled", "certificate_id", "locations", "meta"}},
		{reflect.TypeOf(npmapi.CertificateObject{}), []string{"id", "nice_name", "provider", "domain_names", "expires_on"}},
		{reflect.TypeOf(npmapi.RedirectionHostObject{}), []string{"id", "domain_names", "forward_scheme", "forward_domain_name", "forward_http_code", "preserve_path", "ssl_forced", "certificate_id", "enabled"}},
		{reflect.TypeOf(npmapi.DeadHostObject{}), []string{"id", "domain_names", "certificate_id", "ssl_forced", "enabled"}},
		{reflect.TypeOf(npmapi.StreamObject{}), []string{"id", "incoming_port", "forwarding_host", "forwarding_port", "tcp_forwarding", "udp_forwarding", "enabled"}},
		{reflect.TypeOf(npmapi.AccessListObject{}), []string{"id", "name", "satisfy_any", "pass_auth"}},
		// expand=items,clients adds these as their own models.
		{reflect.TypeOf(npmapi.AccessClients{}).Elem(), []string{"directive", "address"}},
		{reflect.TypeOf(npmapi.AccessItems{}).Elem(), []string{"username"}},
		{reflect.TypeOf(npmapi.CreateProxyHostJSONBody{}), jsonKeys(reflect.TypeOf(npmProxyHostRequest{})).list()},
	}
	for _, read := range reads {
		known := jsonKeys(read.model)
		for _, key := range read.keys {
			if !known[key] {
				t.Errorf("%s has no field %q in the vendored NPM spec", read.model.Name(), key)
			}
		}
	}
}
