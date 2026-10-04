package providers

import (
	"context"
	"encoding/pem"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// sshHost is one fake edge: what each SSH operation prints, or the exit code
// it fails with, and what it was sent.
type sshHost struct {
	answers map[string]string
	exits   map[string]int
	sent    map[string][]byte
	asked   []string
}

// sshFleet answers SSH by destination host and operation, the last two argv
// words, and records every call.
type sshFleet map[string]*sshHost

func (f sshFleet) exec(_ context.Context, argv []string, stdin []byte, _ []string) ([]byte, []byte, int, error) {
	if argv[0] != "ssh" {
		return nil, nil, 0, errors.New("not ssh")
	}
	destination, operation := argv[len(argv)-2], argv[len(argv)-1]
	_, host, _ := strings.Cut(destination, "@")
	edge := f[host]
	if edge == nil {
		return nil, []byte("no route to host"), 255, nil
	}
	edge.asked = append(edge.asked, operation)
	if edge.sent == nil {
		edge.sent = map[string][]byte{}
	}
	edge.sent[operation] = stdin
	if code := edge.exits[operation]; code != 0 {
		return nil, []byte("refused"), code, nil
	}
	answer, ok := edge.answers[operation]
	if !ok {
		return nil, []byte("unknown operation"), 2, nil
	}
	return []byte(answer), nil, 0, nil
}

// sshEnv declares SSH connections by ref; host is ref.example.com and role is
// the connection's role, if any.
func sshEnv(roles map[string]string) runtime.Environment {
	env := runtime.Environment{"HQ_CONTROLLER_SSH_DIR": "/run/secrets/controller-ssh"}
	for ref, role := range roles {
		prefix := strings.ToUpper(strings.ReplaceAll(ref, "-", "_"))
		env[prefix+"_CONNECTION_REF"] = ref
		env[prefix+"_HOST"] = ref + ".example.com"
		env[prefix+"_USER"] = "hq"
		env[prefix+"_PORT"] = "22"
		env[prefix+"_HOST_KEY"] = "ssh-ed25519 AAAA"
		if role != "" {
			env[prefix+"_ROLE"] = role
		}
	}
	return env
}

func caddyRegistry(roles map[string]string, fleet sshFleet) *Registry {
	env := sshEnv(roles)
	r := New(env, &fakeHTTP{})
	r.Commands = &Commands{Env: env, Exec: fleet.exec}
	return r
}

func routesOf(t *testing.T, r *Registry) map[string]CaddyRouteRecord {
	t.Helper()
	records, err := r.caddyRoutes(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	found := map[string]CaddyRouteRecord{}
	for _, record := range records {
		route := record.(CaddyRouteRecord)
		found[route.Domain] = route
	}
	return found
}

const caddyNested = `{"apps":{"http":{"servers":{"srv0":{"routes":[
 {"match":[{"host":["Deep.Example.com."]}],"handle":[{"handler":"subroute","routes":[{"handle":[{"handler":"subroute","routes":[{"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"app:8080"}]}]}]}]}]}]},
 {"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"catchall:80"}]}]},
 {"match":[{"host":["one.example.com","two.example.com"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"shared:8080"}]}]},
 {"match":[{"host":["ha.example.com"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"a:8080"},{"dial":"b:8080"}]}]},
 {"match":[{"host":["twice.example.com"]}],"handle":[{"handler":"subroute","routes":[
   {"match":[{"path":["/api/*"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"app:9000"}]}]},
   {"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"app:9000"}]}]}]}]},
 {"match":[{"host":["static.example.com"]}],"handle":[{"handler":"file_server"}]},
 {"match":[{"host":["*.example.net"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"{http.request.host}:443"}]}]},
 {"match":[{"host":["any.example.net"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"{http.request.header.X-Target}"}]}]},
 {"match":[{"host":["intercept.example.com"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"app:7000"}],
   "handle_response":[{"routes":[{"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"fallback:7001"}]}]}]}]}]}
]}}}}}`

func TestCaddyRoutesAreOneRecordPerHostname(t *testing.T) {
	r := caddyRegistry(map[string]string{"edge": "caddy"}, sshFleet{"edge.example.com": {answers: map[string]string{"routes": caddyNested}}})
	routes := routesOf(t, r)
	for _, test := range []struct {
		name, domain, upstream string
		requestedHost          bool
	}{
		{"nested however deep, name normalized", "deep.example.com", "app:8080", false},
		{"one route serving several names, first", "one.example.com", "shared:8080", false},
		{"one route serving several names, second", "two.example.com", "shared:8080", false},
		{"balanced names no single upstream", "ha.example.com", "", false},
		{"the same upstream twice is one", "twice.example.com", "app:9000", false},
		{"Caddy answering itself has no upstream", "static.example.com", "", false},
		{"to the host each request names", "*.example.net", "{http.request.host}:443", true},
		{"another placeholder is kept, not the requested host", "any.example.net", "{http.request.header.X-Target}", false},
		{"a response route is a second destination", "intercept.example.com", "", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			route, ok := routes[test.domain]
			if !ok {
				t.Fatalf("no record for %s in %v", test.domain, routes)
			}
			if route.Upstream != test.upstream || route.ToRequestedHost != test.requestedHost || route.ConnectionRef != "edge" {
				t.Fatalf("%+v", route)
			}
		})
	}
	if len(routes) != 9 {
		t.Errorf("a catch-all route is not a hostname: %v", routes)
	}
}

func TestCaddyHostsAsked(t *testing.T) {
	answer := `{"apps":{"http":{"servers":{"srv0":{"routes":[{"match":[{"host":["status.example.com"]}],"handle":[]}]}}}}}`
	for _, test := range []struct {
		name  string
		roles map[string]string
		fleet sshFleet
		asked []string
		found []string
	}{
		{"every declared Caddy host, and nothing declared for something else",
			map[string]string{"edge": "caddy", "mirror": "caddy", "box": "backup"},
			sshFleet{"edge.example.com": {answers: map[string]string{"routes": answer}}, "mirror.example.com": {answers: map[string]string{"routes": answer}}, "box.example.com": {}},
			[]string{"edge", "mirror"}, []string{"status.example.com", "status.example.com"}},
		{"with no role declared anywhere, every SSH host",
			map[string]string{"edge": "", "box": ""},
			sshFleet{"edge.example.com": {answers: map[string]string{"routes": answer}}, "box.example.com": {exits: map[string]int{"routes": 1}}},
			[]string{"box", "edge"}, []string{"status.example.com"}},
		{"a host that does not answer, or answers with no config, is skipped",
			map[string]string{"edge": "caddy", "down": "caddy", "odd": "caddy"},
			sshFleet{"edge.example.com": {answers: map[string]string{"routes": answer}}, "odd.example.com": {answers: map[string]string{"routes": `{"apps":{"http":{"servers":[]}}}`}}},
			[]string{"edge", "odd"}, []string{"status.example.com"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := caddyRegistry(test.roles, test.fleet)
			records, err := r.caddyRoutes(t.Context())
			if err != nil {
				t.Fatal(err)
			}
			domains := []string{}
			for _, record := range records {
				domains = append(domains, record.(CaddyRouteRecord).Domain)
			}
			if !slices.Equal(domains, test.found) {
				t.Errorf("found %v, want %v", domains, test.found)
			}
			asked := []string{}
			for host, edge := range test.fleet {
				if len(edge.asked) > 0 {
					asked = append(asked, strings.TrimSuffix(host, ".example.com"))
				}
			}
			slices.Sort(asked)
			if !slices.Equal(asked, test.asked) {
				t.Errorf("asked %v, want %v", asked, test.asked)
			}
		})
	}
}

// A host that fails is visible to HQ as a failed step, never silently absent.
func TestCaddySkippedHostIsAFailedStep(t *testing.T) {
	r := caddyRegistry(map[string]string{"down": "caddy", "odd": "caddy"}, sshFleet{
		"down.example.com": {exits: map[string]int{"routes": 255}},
		"odd.example.com":  {answers: map[string]string{"routes": `[]`}},
	})
	if _, err := r.caddyRoutes(t.Context()); err != nil {
		t.Fatal(err)
	}
	steps := []string{}
	for _, failure := range r.Commands.StepFailures() {
		steps = append(steps, failure.Step+": "+failure.Reason)
	}
	want := []string{"SSH routes for down: exit 255", "SSH routes for odd: config did not decode"}
	if !slices.Equal(steps, want) {
		t.Fatalf("steps %v", steps)
	}
}

func caddyLoading(files int, hosts ...string) string {
	loads := []string{}
	for i := range files {
		loads = append(loads, `{"certificate":"/certs/`+string(rune('a'+i))+`/fullchain.pem","key":"/certs/k.pem"}`)
	}
	routes := []string{}
	for _, host := range hosts {
		routes = append(routes, `{"match":[{"host":["`+host+`"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"app:80"}]}]}`)
	}
	return `{"apps":{"http":{"servers":{"srv0":{"routes":[` + strings.Join(routes, ",") + `]}}},"tls":{"certificates":{"load_files":[` + strings.Join(loads, ",") + `]}}}}`
}

func leafPEM(t *testing.T, pki *testPKI, serial int64, notAfter time.Time, names ...string) string {
	t.Helper()
	_, _, der := pki.leaf(serial, notAfter, names...)
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
}

func TestCaddyRouteCertificates(t *testing.T) {
	pki := newTestPKI(t)
	soon, later := time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC), time.Date(2031, 1, 1, 0, 0, 0, 0, time.UTC)
	exact := leafPEM(t, pki, 2, soon, "app.example.com")
	wildcard := leafPEM(t, pki, 3, later, "*.example.com")
	wildcardSooner := leafPEM(t, pki, 4, soon, "*.example.com")
	for _, test := range []struct {
		name     string
		edge     *sshHost
		domain   string
		serial   string // the certificate's expiry chosen, or "" for none
		unread   string
		notAsked bool
	}{
		{"an edge loading no file is not asked", &sshHost{answers: map[string]string{"routes": caddyLoading(0, "app.example.com")}},
			"app.example.com", "", caddyManagesCertificate, true},
		{"the host's own name over a wildcard valid longer", &sshHost{answers: map[string]string{"routes": caddyLoading(2, "app.example.com"), "certificates": wildcard + exact}},
			"app.example.com", stamp(soon), "", false},
		{"of two covering alike, the one valid longest, whatever the order", &sshHost{answers: map[string]string{"routes": caddyLoading(2, "api.example.com"), "certificates": wildcard + wildcardSooner}},
			"api.example.com", stamp(later), "", false},
		{"a wildcard covers one label only", &sshHost{answers: map[string]string{"routes": caddyLoading(1, "a.b.example.com"), "certificates": wildcard}},
			"a.b.example.com", "", caddyManagesCertificate, false},
		{"an older target answers certificate", &sshHost{answers: map[string]string{"routes": caddyLoading(1, "app.example.com"), "certificate": exact}, exits: map[string]int{"certificates": 127}},
			"app.example.com", stamp(soon), "", false},
		{"a target with neither says it must be redeployed", &sshHost{answers: map[string]string{"routes": caddyLoading(1, "app.example.com")}, exits: map[string]int{"certificates": 127, "certificate": 127}},
			"app.example.com", "", caddyNoCertificateOperation, false},
		{"one of two reported names the other unread", &sshHost{answers: map[string]string{"routes": caddyLoading(2, "app.example.com", "other.example.org"), "certificate": exact}, exits: map[string]int{"certificates": 127}},
			"other.example.org", "", "the edge loads 2 certificates from files and its target reports 1; redeploy it at version 4 or later", false},
		{"a certificate that does not parse is said so", &sshHost{answers: map[string]string{"routes": caddyLoading(1, "app.example.com"), "certificates": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"}},
			"app.example.com", "", "its certificate did not parse", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := caddyRegistry(map[string]string{"edge": "caddy"}, sshFleet{"edge.example.com": test.edge})
			route := routesOf(t, r)[test.domain]
			switch {
			case test.serial != "":
				if route.Certificate == nil || route.Certificate.ExpiresOn != test.serial || route.CertificateUnread != "" {
					t.Fatalf("%+v", route)
				}
				if route.Certificate.Provider != "Example CA" || route.Certificate.Name == "" {
					t.Errorf("facts %+v", route.Certificate)
				}
			default:
				if route.Certificate != nil || !strings.HasPrefix(route.CertificateUnread, test.unread) {
					t.Fatalf("%+v", route)
				}
			}
			if asked := slices.Contains(test.edge.asked, "certificates"); asked == test.notAsked {
				t.Errorf("asked %v", test.edge.asked)
			}
		})
	}
}

func TestRenderCaddyRoutes(t *testing.T) {
	rendered, err := renderCaddyRoutes([]CaddyRouteInFile{
		{Domain: "b.example.com", Upstream: "app:8080"},
		{Domain: "a.example.com", Upstream: "https://backend.example.com:8443"},
		{Domain: "nowhere.example.com"},
	}, "/opt/apps/caddy/certs/")
	if err != nil {
		t.Fatal(err)
	}
	want := caddyFileHeader + "\n" +
		"a.example.com {\n\ttls /opt/apps/caddy/certs/fullchain.pem /opt/apps/caddy/certs/privkey.pem\n\treverse_proxy https://backend.example.com:8443\n}\n\n" +
		"b.example.com {\n\ttls /opt/apps/caddy/certs/fullchain.pem /opt/apps/caddy/certs/privkey.pem\n\treverse_proxy app:8080\n}\n"
	if rendered != want {
		t.Fatalf("rendered\n%s\nwant\n%s", rendered, want)
	}
	plain, err := renderCaddyRoutes([]CaddyRouteInFile{{Domain: "*.example.com", Upstream: "h2c://app:80"}}, "")
	if err != nil || !strings.Contains(plain, "*.example.com {\n\treverse_proxy h2c://app:80\n}") || strings.Contains(plain, "tls ") {
		t.Fatalf("%v\n%s", err, plain)
	}

	for _, test := range []struct {
		name      string
		route     CaddyRouteInFile
		directory string
	}{
		{"an upstream that would write directives", CaddyRouteInFile{Domain: "a.example.com", Upstream: "app:80\n}\nevil.example.com {\n\tfile_server"}, ""},
		{"a domain that would write directives", CaddyRouteInFile{Domain: "a.example.com {\n\timport /etc/passwd\n}\nb.example.com", Upstream: "app:80"}, ""},
		{"a placeholder never reaches the file", CaddyRouteInFile{Domain: "a.example.com", Upstream: "{http.request.host}:443"}, ""},
		{"a second directive on the line", CaddyRouteInFile{Domain: "a.example.com", Upstream: "app:8080\r\nimport /etc/passwd"}, ""},
		{"two upstreams", CaddyRouteInFile{Domain: "a.example.com", Upstream: "app:8080 app:9090"}, ""},
		{"a quoted upstream", CaddyRouteInFile{Domain: "a.example.com", Upstream: `"app:8080"`}, ""},
		{"two hostnames", CaddyRouteInFile{Domain: "a.example.com, b.example.com", Upstream: "app:8080"}, ""},
		{"a directory that is not one plain path", CaddyRouteInFile{Domain: "a.example.com", Upstream: "app:80"}, "/certs\n\troot *"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := renderCaddyRoutes([]CaddyRouteInFile{test.route}, test.directory); err == nil {
				t.Fatal("rendered")
			}
		})
	}
	// The directory is checked even when no route would use it.
	if _, err := renderCaddyRoutes(nil, "relative/certs"); err == nil {
		t.Fatal("an invalid certificate directory with no routes was accepted")
	}
}

func TestCaddyReconcile(t *testing.T) {
	edge := &sshHost{answers: map[string]string{"routes:write": ""}}
	r := caddyRegistry(map[string]string{"edge": "caddy"}, sshFleet{"edge.example.com": edge})
	spec := Object{"connection_ref": "edge", "domain": "a.example.com", "upstream": "app:80", "certificate_directory": "",
		"routes": []any{Object{"domain": "a.example.com", "upstream": "app:80"}}}

	plan, err := r.runAction(runtime.ResourceKindCaddyRoute, "reconcile", t.Context(), spec, nil, false)
	if err != nil || plan.Changed || plan.Status.(CaddyRouteStatus).Routes != 1 || len(edge.asked) != 0 {
		t.Fatalf("a plan writes nothing: %v %+v %v", err, plan, edge.asked)
	}
	applied, err := r.runAction(runtime.ResourceKindCaddyRoute, "reconcile", t.Context(), spec, nil, true)
	if err != nil || !applied.Changed || applied.Conditions[0].Reason != "Written" {
		t.Fatalf("%v %+v", err, applied)
	}
	if want := caddyFileHeader + "\na.example.com {\n\treverse_proxy app:80\n}\n"; string(edge.sent["routes:write"]) != want {
		t.Fatalf("wrote %q", edge.sent["routes:write"])
	}

	bad := Object{"connection_ref": "edge", "routes": []any{Object{"domain": "a.example.com", "upstream": "app:80 {"}}}
	if _, err := r.runAction(runtime.ResourceKindCaddyRoute, "reconcile", t.Context(), bad, nil, false); err == nil {
		t.Fatal("a plan renders, and so refuses, what the file writer refuses")
	}
	edge.exits = map[string]int{"routes:write": 1}
	if _, err := r.runAction(runtime.ResourceKindCaddyRoute, "reconcile", t.Context(), spec, nil, true); err == nil {
		t.Fatal("a write the edge refused reported as written")
	}
}
