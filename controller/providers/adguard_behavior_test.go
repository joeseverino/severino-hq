package providers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// adguardServer answers AdGuard paths from fixed bodies; a status refuses the path.
func adguardServer(t *testing.T, bodies map[string]string, refuse map[string]int) *Registry {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if code, ok := refuse[req.URL.Path]; ok {
			w.WriteHeader(code)
			return
		}
		body, ok := bodies[req.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, body)
	}))
	t.Cleanup(server.Close)
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	return New(runtime.Environment{"ADGUARD_CONNECTION_REF": "dns", "ADGUARD_URL": server.URL, "ADGUARD_USERNAME": "user", "ADGUARD_PASSWORD": "synthetic"}, client)
}

// dns_addresses null is AdGuard listening on no address: the probe still
// passes and the record reports [], the same as an empty or absent list.
func TestAdGuardNullDNSAddressesMeansNoneListening(t *testing.T) {
	for name, status := range map[string]string{
		"null":   `{"version":"v0.107.0","running":true,"dns_addresses":null}`,
		"empty":  `{"version":"v0.107.0","running":true,"dns_addresses":[]}`,
		"absent": `{"version":"v0.107.0","running":true}`,
	} {
		t.Run(name, func(t *testing.T) {
			r := adguardServer(t, map[string]string{"/control/status": status}, nil)
			done := r.BeginSnapshot()
			defer done()
			probe, err := r.adguardProbe(context.Background(), "dns")
			if err != nil || probe.Detail != "AdGuard v0.107.0" {
				t.Fatalf("%+v %v", probe, err)
			}
			records, err := r.adguardDNS(context.Background())
			if err != nil || len(records) != 1 {
				t.Fatalf("%v %v", records, err)
			}
			if got := records[0].(AdGuardDNSRecord).DNSAddresses; got == nil || len(got) != 0 {
				t.Errorf("dns_addresses = %#v, want []", got)
			}
		})
	}
}

func TestAdGuardProbeFailures(t *testing.T) {
	cases := []struct {
		name    string
		status  string
		refuse  int
		failure runtime.FailureClass
	}{
		{"an answer without a version is not AdGuard", `{"dns_addresses":["192.0.2.53"]}`, 0, runtime.FailureClassAddress},
		{"a refused credential", "", http.StatusUnauthorized, runtime.FailureClassCredential},
		{"a missing permission", "", http.StatusForbidden, runtime.FailureClassPermission},
		{"a malformed status", `{"version":7}`, 0, runtime.FailureClassUnclassified},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			refuse := map[string]int{}
			if c.refuse != 0 {
				refuse["/control/status"] = c.refuse
			}
			r := adguardServer(t, map[string]string{"/control/status": c.status}, refuse)
			_, err := r.adguardProbe(context.Background(), "dns")
			failure, _, _ := runtime.Classify(err)
			if err == nil || failure != c.failure {
				t.Errorf("err = %v (%q), want class %q", err, failure, c.failure)
			}
		})
	}
}

func TestAdGuardMalformedRewriteListIsAnError(t *testing.T) {
	for name, body := range map[string]string{
		"not a list":        `{"domain":"a.example"}`,
		"a domain not text": `[{"domain":5,"answer":"192.0.2.1"}]`,
		"enabled not bool":  `[{"domain":"a.example","answer":"192.0.2.1","enabled":"yes"}]`,
	} {
		t.Run(name, func(t *testing.T) {
			r := adguardServer(t, map[string]string{"/control/rewrite/list": body}, nil)
			_, err := r.runAction(runtime.ResourceKindAdGuardRewrite, "reconcile", context.Background(), Object{"domain": "a.example", "answer": "192.0.2.1"}, nil, false)
			if err == nil {
				t.Error("decoded a malformed rewrite list")
			}
		})
	}
}

func TestAdGuardQueryLogEntryWithoutATimeIsAnError(t *testing.T) {
	get := func(string) (json.RawMessage, error) {
		return encoded(Object{"data": []Object{{"time": "yesterday", "question": Object{"name": "example.test"}}}})
	}
	if _, err := summarizeQueries(get, []string{"example.test"}, "dns", time.Now(), false); err == nil {
		t.Error("an entry whose time does not parse was skipped, not refused")
	}
}

func TestAdGuardQueryLogRetentionIsHours(t *testing.T) {
	r := adguardServer(t, map[string]string{
		"/control/status":           `{"version":"v0.107.0","dns_addresses":[]}`,
		"/control/dns_info":         `{"upstream_dns":["tls://dns.example"],"upstream_mode":"load_balance"}`,
		"/control/filtering/status": `{"enabled":true,"filters":[{"enabled":true,"rules_count":10},{"enabled":false,"rules_count":5}]}`,
		"/control/querylog/config":  `{"enabled":true,"interval":5400000,"anonymize_client_ip":false}`,
		"/control/rewrite/settings": `{"enabled":true}`,
	}, nil)
	done := r.BeginSnapshot()
	defer done()
	records, err := r.adguardDNS(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	record := records[0].(AdGuardDNSRecord)
	if got := record.QuerylogRetentionHours; got == nil || *got != 1.5 {
		t.Errorf("retention %v", got)
	}
	if record.FilterLists != 1 || record.FilterRules != 10 {
		t.Errorf("%+v", record.AdGuardFilteringPart)
	}
	if !reflect.DeepEqual(record.Upstreams, []AdGuardUpstream{{Host: "dns.example", Transport: "tls", Domains: []string{}}}) {
		t.Errorf("%+v", record.Upstreams)
	}
}

func TestRoundToRoundsHalvesAwayFromZero(t *testing.T) {
	for _, c := range []struct {
		x      float64
		places int
		want   float64
	}{{2.25, 1, 2.3}, {2.35, 1, 2.4}, {0.125, 2, 0.13}, {24, 1, 24}} {
		if got := roundTo(c.x, c.places); got != c.want {
			t.Errorf("roundTo(%v, %d) = %v, want %v", c.x, c.places, got, c.want)
		}
	}
}
