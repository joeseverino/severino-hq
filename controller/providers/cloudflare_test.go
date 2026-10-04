package providers

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The query is sent verbatim; its digest is the Python controller's.
func TestAnalyticsQueryMatchesPython(t *testing.T) {
	query := analyticsQuery()
	sum := sha256.Sum256([]byte(query))
	if len(query) != 2398 || hex.EncodeToString(sum[:]) != "064389cfa7df9d4cb201d367e649912573cbf522c4ee5d111bd828f19ee5a816" {
		t.Fatalf("query drifted from the Python controller's: %d bytes, %x", len(query), sum)
	}
}

func TestCloudflareRefusal(t *testing.T) {
	cases := []struct {
		detail   string
		status   int
		verified bool
		want     runtime.Refusal
	}{
		{"Invalid API Token", 400, false, runtime.RefusalCredential},
		{"Authentication error", 403, false, runtime.RefusalPermission},
		{"Authentication error", 401, true, runtime.RefusalPermission},
		{"Authentication error", 401, false, runtime.RefusalCredential},
		{"no reason given", 401, true, runtime.RefusalCredential},
		{"no reason given", 403, false, runtime.RefusalPermission},
		{"Record already exists", 400, false, runtime.RefusalUnclassified},
	}
	for _, c := range cases {
		if got := cloudflareRefusal(c.detail, c.status, func() bool { return c.verified }); got != c.want {
			t.Errorf("cloudflareRefusal(%q, %d) = %q, want %q", c.detail, c.status, got, c.want)
		}
	}
}

func TestNormalizedRecordContent(t *testing.T) {
	cases := map[[2]string]string{
		{"TXT", "v=spf1 -all"}:                   `"v=spf1 -all"`,
		{"TXT", `"quoted"`}:                      `"quoted"`,
		{"CNAME", " Target.Example.COM. "}:       "target.example.com",
		{"CAA", `0   issue   "letsencrypt.org"`}: `0 issue "letsencrypt.org"`,
		{"A", " 192.0.2.1 "}:                     "192.0.2.1",
	}
	for input, want := range cases {
		if got := normalizedRecordContent(input[0], input[1]); got != want {
			t.Errorf("normalizedRecordContent(%q, %q) = %q, want %q", input[0], input[1], got, want)
		}
	}
	if _, _, _, ok := caaParts("0 issue letsencrypt.org"); ok {
		t.Error("an unquoted CAA value parsed")
	}
}

func TestRedirectHosts(t *testing.T) {
	got := expressionHosts(`(http.host eq "WWW.Example.com" or http.host in {"a.example" "*.b.example"}) and http.request.full_uri wildcard r"https://c.example/*"`)
	want := []string{"www.example.com", "a.example", "*.b.example", "c.example"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("expressionHosts = %v, want %v", got, want)
	}
	if host := targetHost("https://User@Example.com:8443/path"); host != "example.com" {
		t.Errorf("targetHost = %q", host)
	}
	if host := targetHost(`concat("https://dest.example", http.request.uri.path)`); host != "dest.example" {
		t.Errorf("targetHost of an expression = %q", host)
	}
}

func TestPyQuoteAndText(t *testing.T) {
	if got := pyQuote("a b/c+d~e"); got != "a%20b%2Fc%2Bd~e" {
		t.Errorf("pyQuote = %q", got)
	}
	for raw, want := range map[string]string{`"x"`: "x", `5`: "5", `1.5`: "1.5", `true`: "True", `null`: "None", `{"a": [1, "b"]}`: "{'a': [1, 'b']}"} {
		if got := rawText(json.RawMessage(raw)); got != want {
			t.Errorf("rawText(%s) = %q, want %q", raw, got, want)
		}
	}
	if pyOrText(json.RawMessage(`0`)) != "" || pyGetText(nil, "fallback") != "fallback" {
		t.Error("falsy and absent values")
	}
}
