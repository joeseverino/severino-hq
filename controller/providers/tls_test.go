package providers

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

var tlsNow = time.Date(2026, 1, 2, 12, 0, 0, 0, time.UTC)

type tlsHarness struct {
	r       *Registry
	http    *fakeHTTP
	dialer  *fakeDialer
	command *fakeCommander
	clock   time.Time
}

func newTLSHarness(t *testing.T, env runtime.Environment) *tlsHarness {
	t.Helper()
	h := &fakeHTTP{routes: map[string]any{"/api/tokens": Object{"token": "synthetic"}}, fail: map[string]error{}, answers: map[string]any{}, writeFail: map[string]error{}}
	r := New(env, h)
	harness := &tlsHarness{r: r, http: h, clock: tlsNow}
	harness.dialer = &fakeDialer{http: h, certs: map[string][]byte{}}
	harness.command = &fakeCommander{http: h, outcomes: map[string]fakeOutcome{}}
	r.TLS, r.Commands = harness.dialer, &Commands{Env: r.Env, Exec: harness.command.exec}
	r.Now = func() time.Time { return tlsNow }
	r.Monotonic = func() time.Time { return harness.clock }
	r.Sleep = func(_ context.Context, d time.Duration) error { harness.clock = harness.clock.Add(d); return nil }
	return harness
}

func tlsEnv(t *testing.T) runtime.Environment {
	dir := t.TempDir()
	return runtime.Environment{
		"NPM_CONNECTION_REF": "npm", "NPM_URL": "https://example.invalid", "NPM_USERNAME": "user", "NPM_PASSWORD": "synthetic",
		"CADDY_CONNECTION_REF": "caddy", "CADDY_HOST": "edge.example", "CADDY_USER": "deploy", "CADDY_PORT": "22", "CADDY_HOST_KEY": "ssh-ed25519 AAAA",
		"HQ_CONTROLLER_SSH_DIR": filepath.Join(dir, "ssh"), "HQ_ACME_DIR": filepath.Join(dir, "acme"),
	}
}

func verified(ctx context.Context) context.Context {
	return runtime.WithVerification(ctx, &runtime.Verification{TimeoutSeconds: 30, IntervalSeconds: 5})
}

func TestValidateCertificate(t *testing.T) {
	pki := newTestPKI(t)
	chain, key, der := pki.leaf(2, tlsNow.AddDate(0, 3, 0), "a.example", "*.a.example")
	digest := sha256.Sum256(der)
	got, err := validateCertificate(chain, key, []string{"a.example", "*.a.example"})
	if err != nil || got != strings.ToLower(base16(digest[:])) {
		t.Fatalf("fingerprint %q, %v", got, err)
	}
	if _, err := validateCertificate(chain, key, []string{"a.example", "b.example", "c.example"}); err == nil || err.Error() != "issued certificate is missing names: b.example, c.example" {
		t.Fatalf("missing names: %v", err)
	}
	_, other, _ := pki.leaf(3, tlsNow.AddDate(0, 3, 0), "a.example")
	if _, err := validateCertificate(chain, other, []string{"a.example"}); err == nil || err.Error() != "certificate and private key do not match" {
		t.Fatalf("mismatch: %v", err)
	}
	if _, err := validateCertificate([]byte("not a certificate"), key, nil); err == nil || !strings.HasPrefix(err.Error(), "certificate unreadable") {
		t.Fatalf("bad certificate: %v", err)
	}
	if _, err := validateCertificate(chain, []byte("not a key"), nil); err == nil || !strings.HasPrefix(err.Error(), "private key unreadable") {
		t.Fatalf("bad key: %v", err)
	}
}

func base16(data []byte) string {
	const digits = "0123456789abcdef"
	out := make([]byte, 0, len(data)*2)
	for _, b := range data {
		out = append(out, digits[b>>4], digits[b&15])
	}
	return string(out)
}

func TestBundleRoundTrip(t *testing.T) {
	fullchain, key, err := readBundle(certificateBundle([]byte("chain"), []byte("key")))
	if err != nil || string(fullchain) != "chain" || string(key) != "key" {
		t.Fatalf("round trip: %q %q %v", fullchain, key, err)
	}
	for payload, want := range map[string]string{
		"":          "certificate snapshot was invalid",
		"not a tar": "certificate snapshot was invalid",
	} {
		if _, _, err := readBundle([]byte(payload)); err == nil || err.Error() != want {
			t.Fatalf("%q: %v", payload, err)
		}
	}
}

func TestDaysUntilAndCovers(t *testing.T) {
	for _, tc := range []struct {
		when time.Time
		want int
	}{{tlsNow.Add(90 * 24 * time.Hour), 90}, {tlsNow.Add(time.Second), 1}, {tlsNow, 0}, {tlsNow.Add(-time.Hour), -1}} {
		if got := daysUntil(tc.when, tlsNow); got != tc.want {
			t.Fatalf("daysUntil(%v) = %d, want %d", tc.when, got, tc.want)
		}
	}
	names := nameSet([]string{"*.example.test", "example.test"})
	if !certificateCovers("A.Example.Test.", names) || !certificateCovers("example.test", names) || certificateCovers("a.b.example.test", names) {
		t.Fatal("wildcard covers one label")
	}
}

func TestReconcileTLSReadsEveryConsumer(t *testing.T) {
	h := newTLSHarness(t, tlsEnv(t))
	pki := newTestPKI(t)
	_, _, current := pki.leaf(2, tlsNow.AddDate(0, 3, 0), "a.example")
	_, _, stale := pki.leaf(3, tlsNow.AddDate(0, 0, 10), "a.example")
	h.dialer.certs = map[string][]byte{"current": current, "stale": stale}
	h.dialer.phases = []map[string]fakeServe{{
		"edge.example|a.example":    {Cert: "current"},
		"edge.example|b.example":    {Error: "connection refused"},
		"example.invalid|a.example": {Cert: "stale"},
	}}
	spec := Object{"certificate_name": "a", "domains": []any{"a.example"}, "renewal_window_days": 30, "consumers": []any{
		Object{"kind": "caddy", "name": "edge", "connection_ref": "caddy", "verify_domains": []any{"a.example", "b.example"}},
		Object{"kind": "npm", "name": "proxy", "connection_ref": "npm", "verify_domains": []any{"a.example"}},
		Object{"kind": "cpanel", "name": "host", "connection_ref": "nope", "verify_domains": []any{}},
	}}
	result, err := h.r.runAction(runtime.ResourceKindTLSCertificate, "reconcile", context.Background(), spec, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	reasons := []string{}
	for _, c := range result.Conditions {
		reasons = append(reasons, c.Reason)
	}
	if strings.Join(reasons, ",") != "ConsumerMismatch,ExpiringSoon,ConsumerUnverified,ConsumerUnreachable" {
		t.Fatalf("conditions: %v", reasons)
	}
	status := result.Status.(*TLSCertificateStatus)
	if len(status.UnreachableConsumers) != 1 || status.UnreachableConsumers[0].Reason != "TLS read of b.example: connection refused" {
		t.Fatalf("unreachable: %+v", status.UnreachableConsumers)
	}
	data, _ := json.Marshal(status)
	if strings.Contains(string(data), "matches_expected") || !strings.Contains(string(data), `"certificate_pem":"-----BEGIN CERTIFICATE-----`) {
		t.Fatalf("status shape: %s", data)
	}
}

func TestRenewalDeploysVerifiesAndRollsBack(t *testing.T) {
	env := tlsEnv(t)
	h := newTLSHarness(t, env)
	pki := newTestPKI(t)
	oldChain, oldKey, oldDER := pki.leaf(2, tlsNow.AddDate(0, 0, 5), "a.example")
	newChain, newKey, newDER := pki.leaf(3, tlsNow.AddDate(0, 3, 0), "a.example")
	h.dialer.certs = map[string][]byte{"old": oldDER, "new": newDER}
	h.command.outcomes = map[string]fakeOutcome{
		"ssh snapshot":     {StdoutB64: base64.StdEncoding.EncodeToString(certificateBundle(oldChain, oldKey))},
		"ssh deploy":       {},
		"certbot certonly": {},
	}
	h.command.lineage = map[string]string{"fullchain.pem": string(newChain), "privkey.pem": string(newKey)}
	env["CLOUDFLARE_DNS_API_TOKEN"], env["ACME_EMAIL"], env["ACME_DIRECTORY_URL"] = "synthetic", "ops@example.test", "https://acme.example/directory"
	_ = os.MkdirAll(env["HQ_ACME_DIR"], 0o700)
	spec := Object{"certificate_name": "a", "domains": []any{"a.example"}, "renewal_window_days": 30, "consumers": []any{
		Object{"kind": "caddy", "name": "edge", "connection_ref": "caddy", "verify_domains": []any{"a.example"}},
	}}

	h.dialer.phases = []map[string]fakeServe{{"edge.example|a.example": {Cert: "old"}}, {"edge.example|a.example": {Cert: "new"}}}
	result, err := h.r.runAction(runtime.ResourceKindTLSCertificate, "renew", verified(context.Background()), spec, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	status := result.Status.(*TLSCertificateStatus)
	if status.ArtifactSource != "new_issuance" || !*status.Consumers[0].MatchesExpected || result.Message != "Certificate renewed, deployed, and verified." {
		t.Fatalf("renewal: %+v", status)
	}

	// The new certificate never activates: verification times out and the old one is restored.
	h2 := newTLSHarness(t, env)
	h2.dialer.certs, h2.command.outcomes, h2.command.lineage = h.dialer.certs, h.command.outcomes, h.command.lineage
	_ = os.RemoveAll(filepath.Join(env["HQ_ACME_DIR"], "config"))
	h2.dialer.phases = []map[string]fakeServe{{"edge.example|a.example": {Cert: "old"}}}
	_, err = h2.r.runAction(runtime.ResourceKindTLSCertificate, "renew", verified(context.Background()), spec, nil, true)
	want := "certificate deployment failed, rollback succeeded: 1 of 1 TLS consumers did not activate the certificate within 30s: edge still serves the previous certificate at a.example"
	if err == nil || err.Error() != want {
		t.Fatalf("rollback: %v", err)
	}
	if _, err := h2.r.runAction(runtime.ResourceKindTLSCertificate, "renew", context.Background(), spec, nil, true); err == nil {
		t.Fatal("a renewal without a declared verification policy must refuse")
	}
}

func TestCPanelPlanRefusesUnservedNames(t *testing.T) {
	env := tlsEnv(t)
	env["HOST_CONNECTION_REF"], env["HOST_HOST"], env["HOST_USER"], env["HOST_PORT"], env["HOST_HOST_KEY"] = "cpanel", "cp.example", "acct", "22", "ssh-ed25519 AAAA"
	h := newTLSHarness(t, env)
	h.command.outcomes["ssh sites"] = fakeOutcome{Stdout: `{"sites":{"a.example":["www.a.example"],"b.example":null}}`}
	consumer := TLSConsumer{Kind: "cpanel", Name: "host", ConnectionRef: "cpanel", VerifyDomains: []string{"WWW.a.example", "b.example"}, InstallDomains: []string{"a.example"}}
	_, err := h.r.cpanelSitesFor(context.Background(), consumer)
	want := "host would be checked at b.example but installs only on a.example; add those names to the target's install list, or leave it empty to install on every site that serves a checked name"
	if err == nil || err.Error() != want {
		t.Fatalf("plan: %v", err)
	}
	consumer.InstallDomains = nil
	sites, err := h.r.cpanelSitesFor(context.Background(), consumer)
	if err != nil || strings.Join(sites, ",") != "a.example,b.example" {
		t.Fatalf("sites: %v %v", sites, err)
	}
}

func observedOf(raw Object) TLSCertificateObserved {
	observed, _ := decodePayload[TLSCertificateObserved](raw)
	return observed
}

func TestNPMCertificateIDs(t *testing.T) {
	spec := TLSCertificateSpec{Consumers: []TLSConsumer{{Kind: "npm", Name: "one"}, {Kind: "caddy", Name: "edge"}, {Kind: "npm", Name: "two"}}}
	known := npmCertificateIDsOf(spec, observedOf(Object{"npm_certificate_ids": Object{"two": json.Number("7"), "one": json.Number("5"), "bogus": true}}))
	data, _ := json.Marshal(known)
	if string(data) != `{"one":5,"two":7}` {
		t.Fatalf("ids: %s", data)
	}
	single := npmCertificateIDsOf(TLSCertificateSpec{Consumers: []TLSConsumer{{Kind: "npm", Name: "one"}}}, observedOf(Object{"npm_certificate_id": float64(9)}))
	if len(single) != 1 || single[0].ID != 9 {
		t.Fatalf("single: %+v", single)
	}
	if got := npmCertificateIDsOf(spec, observedOf(Object{"npm_certificate_id": 9.5})); len(got) != 0 {
		t.Fatalf("a float is not an id: %+v", got)
	}
}

func TestCertbotNamesAreCheckedBeforeTheyReachArgv(t *testing.T) {
	for _, domains := range [][]string{{"a.example", "--server=x"}, {"-d"}, {"a.example\n"}} {
		if _, err := checkedDomains(TLSCertificateSpec{Domains: domains}); err == nil {
			t.Fatalf("accepted %q", domains)
		}
	}
	if got, err := checkedDomains(TLSCertificateSpec{Domains: []string{"a.example", "*.a.example"}}); err != nil || len(got) != 2 {
		t.Fatalf("%v %v", got, err)
	}
	r := New(runtime.Environment{"HQ_ACME_DIR": "/acme"}, &fakeHTTP{})
	for _, name := range []string{"../escape", "-x", "A", ""} {
		if _, err := r.lineagePath(TLSCertificateSpec{CertificateName: name}); err == nil {
			t.Fatalf("accepted %q", name)
		}
	}
}

// Stage 1 M2: a renewal is verified only when every consumer was read and
// serves the new certificate. One matching consumer never vouches for another
// that was unreachable or had nothing to check.
func TestRenewalNotVerifiedWhileAConsumerIsUnreadOrUnchecked(t *testing.T) {
	edge := Object{"kind": "caddy", "name": "edge", "connection_ref": "caddy", "verify_domains": []any{"a.example"}}
	cases := []struct {
		name   string
		second Object
		served map[string]fakeServe
		reason string
	}{
		{"unreachable consumer", Object{"kind": "caddy", "name": "mirror", "connection_ref": "caddy", "verify_domains": []any{"b.example"}},
			map[string]fakeServe{"edge.example|a.example": {Cert: "new"}}, "mirror could not be read at b.example"},
		{"unchecked consumer", Object{"kind": "caddy", "name": "mirror", "connection_ref": "caddy", "verify_domains": []any{}},
			map[string]fakeServe{"edge.example|a.example": {Cert: "new"}}, "mirror has no verification domain"},
		{"stale consumer", Object{"kind": "caddy", "name": "mirror", "connection_ref": "caddy", "verify_domains": []any{"b.example"}},
			map[string]fakeServe{"edge.example|a.example": {Cert: "new"}, "edge.example|b.example": {Cert: "old"}}, "mirror still serves the previous certificate at b.example"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			env := tlsEnv(t)
			h := newTLSHarness(t, env)
			pki := newTestPKI(t)
			oldChain, oldKey, oldDER := pki.leaf(2, tlsNow.AddDate(0, 0, 5), "a.example", "b.example")
			newChain, newKey, newDER := pki.leaf(3, tlsNow.AddDate(0, 3, 0), "a.example", "b.example")
			h.dialer.certs = map[string][]byte{"old": oldDER, "new": newDER}
			h.command.outcomes = map[string]fakeOutcome{
				"ssh snapshot":     {StdoutB64: base64.StdEncoding.EncodeToString(certificateBundle(oldChain, oldKey))},
				"ssh deploy":       {},
				"certbot certonly": {},
			}
			h.command.lineage = map[string]string{"fullchain.pem": string(newChain), "privkey.pem": string(newKey)}
			env["CLOUDFLARE_DNS_API_TOKEN"], env["ACME_EMAIL"], env["ACME_DIRECTORY_URL"] = "synthetic", "ops@example.test", "https://acme.example/directory"
			_ = os.MkdirAll(env["HQ_ACME_DIR"], 0o700)
			h.dialer.phases = []map[string]fakeServe{{"edge.example|a.example": {Cert: "old"}, "edge.example|b.example": {Cert: "old"}}, c.served}
			spec := Object{"certificate_name": "a", "domains": []any{"a.example", "b.example"}, "renewal_window_days": 30, "consumers": []any{edge, c.second}}
			_, err := h.r.runAction(runtime.ResourceKindTLSCertificate, "renew", verified(context.Background()), spec, nil, true)
			if err == nil || !strings.Contains(err.Error(), "rollback succeeded") || !strings.Contains(err.Error(), c.reason) {
				t.Fatalf("%v", err)
			}
		})
	}
}

func TestConsumersServe(t *testing.T) {
	spec := TLSCertificateSpec{Consumers: []TLSConsumer{{Name: "edge"}, {Name: "mirror"}}}
	read := func(consumer, fingerprint string) TLSObservation {
		return TLSObservation{Consumer: consumer, FingerprintSHA256: fingerprint}
	}
	cases := []struct {
		name   string
		status TLSCertificateStatus
		want   bool
	}{
		{"every consumer serves it", TLSCertificateStatus{Consumers: []TLSObservation{read("edge", "new"), read("mirror", "new")}}, true},
		{"one unread", TLSCertificateStatus{Consumers: []TLSObservation{read("edge", "new")}}, false},
		{"one unreachable", TLSCertificateStatus{Consumers: []TLSObservation{read("edge", "new"), read("mirror", "new")}, UnreachableConsumers: []TLSUnreachable{{Consumer: "mirror", Domain: "c.example"}}}, false},
		{"one stale", TLSCertificateStatus{Consumers: []TLSObservation{read("edge", "new"), read("mirror", "old")}}, false},
		{"all serve another", TLSCertificateStatus{Consumers: []TLSObservation{read("edge", "old"), read("mirror", "old")}}, false},
	}
	for _, c := range cases {
		if got := consumersServe(spec, &c.status, "new"); got != c.want {
			t.Errorf("%s: %v", c.name, got)
		}
	}
}

// A TLS read that fails says why in Go's words, never with an exception name.
func TestTLSReadFailuresAreClassified(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{&net.DNSError{Err: "no such host", Name: "x"}, "name does not resolve"},
		{&net.OpError{Op: "dial", Err: syscall.ECONNREFUSED}, "connection refused"},
		{context.DeadlineExceeded, "timed out"},
		{errors.New("other"), "connection failed"},
	}
	for _, c := range cases {
		if got := dialFailure(c.err); got.Error() != c.want || !errors.Is(got, c.err) {
			t.Errorf("%v: %q", c.err, got)
		}
	}
	if got := handshakeFailure(x509.UnknownAuthorityError{}); got.Error() != "certificate not trusted for this name" {
		t.Errorf("%q", got)
	}
	if got := handshakeFailure(io.EOF); got.Error() != "connection closed during handshake" {
		t.Errorf("%q", got)
	}
}

// The TLS observer verifies chain and name against the configured roots: a
// certificate the controller does not trust is refused, one it does is read.
func TestTLSObserverVerifiesTheServedCertificate(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	address := server.Listener.Addr().String()
	_, err := NetTLSDialer{}.peerAt(context.Background(), "example.com", address)
	var read *tlsReadError
	if !errors.As(err, &read) || read.reason != "certificate not trusted for this name" {
		t.Fatalf("untrusted: %v", err)
	}
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	_ = os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0o600)
	der, err := NetTLSDialer{CAFile: caFile}.peerAt(context.Background(), "example.com", address)
	if err != nil || len(der) == 0 {
		t.Fatalf("trusted: %v", err)
	}
	if _, err := (NetTLSDialer{CAFile: caFile}).peerAt(context.Background(), "other.example", address); !errors.As(err, &read) {
		t.Fatalf("a name the certificate does not cover is refused: %v", err)
	}
}

func TestTLSFailureStagePrecedence(t *testing.T) {
	cases := []struct {
		name     string
		classify func(error) *tlsReadError
		cause    error
		want     string
	}{
		{"DNS before timeout", dialFailure, errors.Join(&net.DNSError{Err: "missing", Name: "example.com"}, context.DeadlineExceeded), "name does not resolve"},
		{"refused before reset", dialFailure, errors.Join(syscall.ECONNREFUSED, syscall.ECONNRESET), "connection refused"},
		{"certificate before reset", handshakeFailure, errors.Join(x509.UnknownAuthorityError{}, syscall.ECONNRESET), "certificate not trusted for this name"},
		{"closed before timeout", handshakeFailure, errors.Join(io.EOF, context.DeadlineExceeded), "connection closed during handshake"},
		{"TCP reset before timeout", dialFailure, errors.Join(syscall.ECONNRESET, context.DeadlineExceeded), "connection reset"},
		{"TLS reset before timeout", handshakeFailure, errors.Join(syscall.ECONNRESET, context.DeadlineExceeded), "connection reset"},
		{"TLS timeout", handshakeFailure, context.DeadlineExceeded, "timed out"},
		{"TLS fallback", handshakeFailure, errors.New("unknown"), "handshake failed"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := c.classify(c.cause)
			if got.Error() != c.want || !errors.Is(got, c.cause) {
				t.Fatalf("classification = %q, want %q with preserved cause", got, c.want)
			}
		})
	}
}
