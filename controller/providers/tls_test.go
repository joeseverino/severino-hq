package providers

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
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
	if _, err := validateCertificate(chain, key, []string{"a.example", "b.example", "c.example"}); err == nil || err.Error() != "Issued certificate is missing names: b.example, c.example." {
		t.Fatalf("missing names: %v", err)
	}
	_, other, _ := pki.leaf(3, tlsNow.AddDate(0, 3, 0), "a.example")
	if _, err := validateCertificate(chain, other, []string{"a.example"}); err == nil || err.Error() != "Certificate and private key do not match." {
		t.Fatalf("mismatch: %v", err)
	}
	if _, err := validateCertificate([]byte("not a certificate"), key, nil); err == nil || err.Error() != "reading the certificate failed." {
		t.Fatalf("bad certificate: %v", err)
	}
	if _, err := validateCertificate(chain, []byte("not a key"), nil); err == nil || err.Error() != "reading the private key failed." {
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
		"":          "Certificate snapshot was invalid.",
		"not a tar": "Certificate snapshot was invalid.",
	} {
		if _, _, err := readBundle([]byte(payload)); err == nil || err.Error() != want {
			t.Fatalf("%q: %v", payload, err)
		}
	}
}

func TestSigningMatchesOpenSSLSemantics(t *testing.T) {
	env := tlsEnv(t)
	env["GITHUB_CONNECTION_REF"] = "app"
	sshDir := env["HQ_CONTROLLER_SSH_DIR"]
	_ = os.MkdirAll(sshDir, 0o700)
	r := New(env, &fakeHTTP{routes: map[string]any{}, fail: map[string]error{}})
	data := []byte("header.payload")
	digest := sha256.Sum256(data)

	rsaKey, _ := rsa.GenerateKey(rand.Reader, 2048)
	rsaDER, _ := x509.MarshalPKCS8PrivateKey(rsaKey)
	_ = os.WriteFile(filepath.Join(sshDir, "app.key"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: rsaDER}), 0o600)
	signature, err := r.Sign(context.Background(), "app", data)
	if err != nil || rsa.VerifyPKCS1v15(&rsaKey.PublicKey, crypto.SHA256, digest[:], signature) != nil {
		t.Fatalf("rsa: %v", err)
	}

	ecKey, _ := ecdsa.GenerateKey(newTestPKI(t).caKey.Curve, rand.Reader)
	ecDER, _ := x509.MarshalECPrivateKey(ecKey)
	_ = os.WriteFile(filepath.Join(sshDir, "app.key"), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: ecDER}), 0o600)
	signature, err = r.Sign(context.Background(), "app", data)
	if err != nil || !ecdsa.VerifyASN1(&ecKey.PublicKey, digest[:], signature) {
		t.Fatalf("ecdsa: %v", err)
	}

	_, edKey, _ := ed25519.GenerateKey(rand.Reader)
	edDER, _ := x509.MarshalPKCS8PrivateKey(edKey)
	_ = os.WriteFile(filepath.Join(sshDir, "app.key"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: edDER}), 0o600)
	if _, err := r.Sign(context.Background(), "app", data); err == nil || err.Error() != "sign for app failed." {
		t.Fatalf("ed25519 must fail like openssl dgst: %v", err)
	}
	if steps := r.commands().StepFailures(); len(steps) != 1 || steps[0].Step != "sign for app" || steps[0].Subject != "app" {
		t.Fatalf("step failures: %+v", steps)
	}
	for ref, want := range map[string]string{"": "Invalid signing connection.", "a/b": "Invalid signing connection.", ".hidden": "Invalid signing connection.", "nope": "No connection named 'nope' was supplied to the controller."} {
		if _, err := r.Sign(context.Background(), ref, data); err == nil || err.Error() != want {
			t.Fatalf("%q: %v", ref, err)
		}
	}
	if _, err := r.SigningPublicKey("app"); err == nil || err.Error() != "No signing key was rendered for 'app'." {
		t.Fatalf("public key: %v", err)
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
		"edge.example|b.example":    {Error: "ConnectionRefusedError"},
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
	if len(status.UnreachableConsumers) != 1 || status.UnreachableConsumers[0].Reason != "TLS observation failed for b.example: ConnectionRefusedError." {
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
	want := "Certificate deployment failed: 1 of 1 TLS consumers did not activate the certificate within 30s: edge still serves the previous certificate at a.example. Rollback succeeded."
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
	want := "host would be checked at b.example but installs only on a.example. Add those names to the target's install list, or leave the list empty to install on every site that serves a checked name."
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

func TestSSHTargetMessages(t *testing.T) {
	env := runtime.Environment{"X_CONNECTION_REF": "x", "X_HOST": "-oProxyCommand=evil", "X_USER": "deploy", "X_PORT": "22", "X_HOST_KEY": "k"}
	if _, err := env.SSH("x"); err == nil || err.Error() != "The host configured for x is not a host name or address." {
		t.Fatalf("host: %v", err)
	}
	env["X_HOST"], env["X_PORT"] = "edge.example", "+22"
	if _, err := env.SSH("x"); err == nil || err.Error() != "The port configured for x is not a port number." {
		t.Fatalf("port: %v", err)
	}
	if _, err := env.SSH("missing"); err == nil || err.Error() != "Unknown certificate transport: missing." {
		t.Fatalf("unknown: %v", err)
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
