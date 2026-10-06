package providers

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// npmServer is an NPM API on httptest: lists answered from bodies, writes
// recorded, any path given a status refused with it.
type npmServer struct {
	*httptest.Server
	mu      sync.Mutex
	lists   map[string]string // path (with query) -> raw JSON body
	refuse  map[string]int    // path -> status
	written []string          // "METHOD path body"
}

func newNPMServer(t *testing.T) *npmServer {
	s := &npmServer{lists: map[string]string{}, refuse: map[string]int{}}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		path := req.URL.Path
		if req.URL.RawQuery != "" {
			path += "?" + req.URL.RawQuery
		}
		body, _ := io.ReadAll(req.Body)
		w.Header().Set("Content-Type", "application/json")
		s.mu.Lock()
		defer s.mu.Unlock()
		if code, ok := s.refuse[path]; ok {
			w.WriteHeader(code)
			return
		}
		switch {
		case path == "/api/tokens":
			io.WriteString(w, `{"token":"short-lived","expires":"2030-01-01T00:00:00Z"}`)
		case req.Method == http.MethodGet:
			raw, ok := s.lists[path]
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			io.WriteString(w, raw)
		default:
			s.written = append(s.written, req.Method+" "+path+" "+string(body))
			io.WriteString(w, `{"id":99}`)
		}
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *npmServer) writes() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string{}, s.written...)
}

// npmAt is the proxy's connection, signing in at url.
func npmAt(url string) connections.Connection {
	return loginConnection(runtime.ConnectionProviderNPM, "proxy", url, "user", "synthetic")
}

func npmRegistry(t *testing.T, held ...connections.Connection) *Registry {
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	return New(runtime.Environment{}, supplied(held...), client)
}

// heldHost is a proxy host as NPM answers it for sampleProxySpec, with flags
// spelled as given.
func heldHost(yes, no string, extra string) string {
	return `[{"id":9,"domain_names":["hq.example"],"forward_scheme":"http","forward_host":"192.0.2.10","forward_port":8000,` +
		`"caching_enabled":` + no + `,"block_exploits":` + yes + `,"allow_websocket_upgrade":` + no + `,"access_list_id":0,` +
		`"certificate_id":0,"ssl_forced":` + no + `,"http2_support":` + yes + `,"hsts_enabled":` + no + `,"hsts_subdomains":` + no + `,` +
		`"trust_forwarded_proto":` + no + `,"advanced_config":"","locations":[],"enabled":` + yes + `,"meta":{}` + extra + `}]`
}

func TestNPMReconcileDecisions(t *testing.T) {
	cases := []struct {
		name     string
		live     string
		observed Object
		write    string // METHOD path, or "" for none
	}{
		{"absent host is created", `[]`, nil, "POST /api/nginx/proxy-hosts"},
		{"matching host is left alone", heldHost("true", "false", ""), nil, ""},
		{"flags written as 1 and 0 match", heldHost("1", "0", ""), nil, ""},
		{"a changed port is updated", strings.Replace(heldHost("true", "false", ""), `"forward_port":8000`, `"forward_port":9000`, 1), nil, "PUT /api/nginx/proxy-hosts/9"},
		{"a renamed host is found by its previous names", strings.Replace(heldHost("true", "false", ""), "hq.example", "old.example", 1), Object{"domain_names": []string{"old.example"}}, "PUT /api/nginx/proxy-hosts/9"},
	}
	for _, c := range cases {
		for _, apply := range []bool{false, true} {
			t.Run(c.name+map[bool]string{false: " (plan)", true: " (apply)"}[apply], func(t *testing.T) {
				server := newNPMServer(t)
				server.lists["/api/nginx/proxy-hosts"] = c.live
				r := npmRegistry(t, npmAt(server.URL))
				res, err := r.runAction(runtime.ResourceKindNPMProxyHost, "reconcile", t.Context(), sampleProxySpec(), c.observed, apply)
				if err != nil {
					t.Fatal(err)
				}
				if res.Changed != (c.write != "") {
					t.Errorf("changed = %v", res.Changed)
				}
				writes := server.writes()
				if !apply || c.write == "" {
					if len(writes) != 0 {
						t.Errorf("wrote %v", writes)
					}
					return
				}
				if len(writes) != 1 || !strings.HasPrefix(writes[0], c.write+" ") {
					t.Errorf("writes %v, want %s", writes, c.write)
				}
			})
		}
	}
}

func TestNPMUpdateCarriesNPMsOwnLocationsAndCertificate(t *testing.T) {
	server := newNPMServer(t)
	held := strings.Replace(heldHost("true", "false", ""), `"locations":[]`, `"locations":[{"path":"/app","forward_scheme":"http","forward_host":"192.0.2.11","forward_port":81}]`, 1)
	held = strings.Replace(held, `"certificate_id":0`, `"certificate_id":"12"`, 1)
	server.lists["/api/nginx/proxy-hosts"] = strings.Replace(held, `"forward_port":8000`, `"forward_port":9000`, 1)
	r := npmRegistry(t, npmAt(server.URL))
	if _, err := r.runAction(runtime.ResourceKindNPMProxyHost, "reconcile", t.Context(), sampleProxySpec(), nil, true); err != nil {
		t.Fatal(err)
	}
	writes := server.writes()
	if len(writes) != 1 {
		t.Fatal(writes)
	}
	var body npmProxyHostRequest
	if err := json.Unmarshal([]byte(strings.SplitN(writes[0], " ", 3)[2]), &body); err != nil {
		t.Fatal(err)
	}
	if body.CertificateID != 12 || !strings.Contains(string(body.Locations), `"/app"`) || body.ForwardPort != 8000 {
		t.Errorf("%+v", body)
	}
}

// An NPM write goes through the connection the spec names, which is the one
// the manages gate approved, never the default connection.
func TestNPMWriteUsesTheNamedConnection(t *testing.T) {
	fallback, named := newNPMServer(t), newNPMServer(t)
	fallback.lists["/api/nginx/proxy-hosts"] = `[]`
	named.lists["/api/nginx/proxy-hosts"] = `[]`
	r := npmRegistry(t, npmAt(fallback.URL), managing(loginConnection(runtime.ConnectionProviderNPM, "proxy-home", named.URL, "user", "synthetic")))
	spec := sampleProxySpec()
	spec["connection_ref"] = "proxy-home"
	for _, action := range []string{"reconcile", "delete"} {
		if _, err := r.runAction(runtime.ResourceKindNPMProxyHost, action, t.Context(), spec, nil, true); err != nil {
			t.Fatalf("%s: %v", action, err)
		}
	}
	if got := fallback.writes(); len(got) != 0 {
		t.Errorf("the default connection was written: %v", got)
	}
	if got := named.writes(); len(got) != 1 || !strings.HasPrefix(got[0], "POST /api/nginx/proxy-hosts ") {
		t.Errorf("named connection writes %v", got)
	}
}

func TestNPMRefusesWithoutWriting(t *testing.T) {
	cases := []struct {
		name string
		live string
		spec func(Object)
		want string
	}{
		{"duplicate hosts", `[` + strings.Trim(heldHost("true", "false", ""), "[]") + `,` + strings.Replace(strings.Trim(heldHost("true", "false", ""), "[]"), `"id":9`, `"id":10`, 1) + `]`, func(Object) {}, npmDuplicateHosts},
		{"https host without a certificate", `[]`, func(s Object) { s["force_ssl"] = true }, "needs an issued certificate"},
		{"forced https on a held host without a certificate", heldHost("true", "false", ""), func(s Object) { s["force_ssl"] = true }, "has no certificate"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			server := newNPMServer(t)
			server.lists["/api/nginx/proxy-hosts"] = c.live
			spec := sampleProxySpec()
			c.spec(spec)
			_, err := npmRegistry(t, npmAt(server.URL)).runAction(runtime.ResourceKindNPMProxyHost, "reconcile", t.Context(), spec, nil, true)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Errorf("err = %v, want %q", err, c.want)
			}
			if w := server.writes(); len(w) != 0 {
				t.Errorf("wrote %v", w)
			}
		})
	}
}

func TestNPMListRefusalsAreClassified(t *testing.T) {
	cases := []struct {
		status  int
		failure runtime.FailureClass
		message string
	}{
		{http.StatusUnauthorized, runtime.FailureClassCredential, "certificate list: credential refused"},
		{http.StatusForbidden, runtime.FailureClassPermission, "certificate list needs certificates: view"},
		{http.StatusInternalServerError, runtime.FailureClassUnclassified, "certificate list: provider answered 500"},
	}
	for _, c := range cases {
		t.Run(http.StatusText(c.status), func(t *testing.T) {
			server := newNPMServer(t)
			server.refuse["/api/nginx/certificates"] = c.status
			r := npmRegistry(t, npmAt(server.URL))
			done := r.BeginSnapshot()
			defer done()
			_, err := r.npmCertificates(t.Context(), "proxy")
			failure, _, _ := runtime.Classify(err)
			if err == nil || failure != c.failure || err.Error() != c.message {
				t.Errorf("err = %v (%q)", err, failure)
			}
		})
	}
}

func TestNPMMalformedAnswersAreErrors(t *testing.T) {
	cases := map[string]string{
		"a flag that is not a flag":     `[{"id":1,"domain_names":["a.example"],"enabled":"yes"}]`,
		"an id that is not a number":    `[{"id":"one","domain_names":["a.example"]}]`,
		"a list that is not a list":     `{"id":1}`,
		"a record that is not a record": `["a.example"]`,
		"a record without an id":        `[{"domain_names":["a.example"],"enabled":true}]`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			server := newNPMServer(t)
			server.lists["/api/nginx/dead-hosts"] = body
			server.lists["/api/nginx/certificates"] = `[]`
			r := npmRegistry(t, npmAt(server.URL))
			done := r.BeginSnapshot()
			defer done()
			if records, err := r.npmDeadHosts(t.Context(), "proxy"); err == nil {
				t.Errorf("decoded %+v", records)
			}
		})
	}
}

func TestNPMProxyHostRecordIsTyped(t *testing.T) {
	server := newNPMServer(t)
	server.lists["/api/nginx/proxy-hosts"] = strings.Replace(heldHost("1", "0", ""), `"enabled":1`, `"enabled":0`, 1)
	server.lists["/api/nginx/access-lists?expand=items,clients"] = `[]`
	server.lists["/api/nginx/certificates"] = `[]`
	r := npmRegistry(t, npmAt(server.URL))
	done := r.BeginSnapshot()
	defer done()
	found, err := r.npmInventory(t.Context())
	if err != nil || len(found) != 1 {
		t.Fatalf("%v %v", found, err)
	}
	record := found[0].(NPMProxyHostRecord)
	if !record.BlockExploits || !record.HTTP2Support || record.SSLForced || record.Enabled || record.ForwardPort != 8000 {
		t.Errorf("%+v", record)
	}
	if got := asJSON(record).(map[string]any)["certificate"]; len(got.(map[string]any)) != 0 {
		t.Errorf("a host without a certificate reports {}, got %v", got)
	}
}
