package providers

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/acme"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// issuanceSpec covers a name and its wildcard: two authorizations, one
// challenge record name.
var issuanceSpec = TLSCertificateSpec{CertificateName: "a", Domains: []string{"a.example", "*.a.example"}}

type issuance struct {
	r         *Registry
	dns       *dnsFake
	authority *acmeFake
	acmeDir   string
	slept     []time.Duration
}

// newIssuance is a Registry whose DNS connection reaches a stateful Cloudflare
// fake and whose ACME connection reaches a fake authority, both through the
// controller's real HTTP clients.
func newIssuance(t *testing.T) *issuance {
	t.Helper()
	pki := newTestPKI(t)
	dns, dnsURL := newDNSFake(t)
	authority := newACMEFake(t, pki, dns)
	env := tlsEnv(t)
	if err := os.MkdirAll(env.ACMEDir, 0o700); err != nil {
		t.Fatal(err)
	}
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	r := New(env, supplied(
		apiTokenConnection(runtime.ConnectionProviderCloudflareDNS, "cloudflare_dns", dnsURL, "synthetic"),
		acmeConnection("acme", "ops@example.test", authority.directory()),
	), client)
	r.Now = func() time.Time { return tlsNow }
	fixture := &issuance{r: r, dns: dns, authority: authority, acmeDir: env.ACMEDir}
	r.Sleep = func(_ context.Context, d time.Duration) error { fixture.slept = append(fixture.slept, d); return nil }
	return fixture
}

func (f *issuance) lineageDir(t *testing.T) string {
	t.Helper()
	dir, err := f.r.lineagePath(issuanceSpec)
	if err != nil {
		t.Fatal(err)
	}
	return dir
}

// certbotLineage lays out a lineage the way certbot left one: live/ names
// links into archive/.
func (f *issuance) certbotLineage(t *testing.T, fullchain, privateKey []byte) {
	t.Helper()
	archive := filepath.Join(f.acmeDir, "config", "archive", "a")
	live := f.lineageDir(t)
	for _, dir := range []string{archive, live} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for name, data := range map[string][]byte{"fullchain": fullchain, "privkey": privateKey, "cert": fullchain, "chain": fullchain} {
		_ = os.WriteFile(filepath.Join(archive, name+"1.pem"), data, 0o600)
		if err := os.Symlink(filepath.Join("..", "..", "archive", "a", name+"1.pem"), filepath.Join(live, name+".pem")); err != nil {
			t.Fatal(err)
		}
	}
}

// stagedLeftovers is anything a save left beside the lineage.
func (f *issuance) stagedLeftovers(t *testing.T) []string {
	t.Helper()
	found, _ := filepath.Glob(filepath.Join(filepath.Dir(f.lineageDir(t)), ".*"))
	return found
}

func TestIssuanceSavesALineageAndRemovesItsChallenges(t *testing.T) {
	f := newIssuance(t)
	oldChain, oldKey, _ := newTestPKI(t).leaf(2, tlsNow.AddDate(0, 0, 5), "a.example", "*.a.example")
	f.certbotLineage(t, oldChain, oldKey)

	fullchain, privateKey, err := f.r.issueCertificate(t.Context(), issuanceSpec)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := validateCertificate(fullchain, privateKey, issuanceSpec.Domains); err != nil {
		t.Fatalf("issued: %v", err)
	}
	savedChain, savedKey, err := readLineage(f.lineageDir(t))
	if err != nil || !bytes.Equal(savedChain, fullchain) || !bytes.Equal(savedKey, privateKey) {
		t.Fatalf("the lineage is not what was issued: %v", err)
	}
	entries, _ := os.ReadDir(f.lineageDir(t))
	modes := map[string]os.FileMode{}
	for _, entry := range entries {
		info, _ := entry.Info()
		modes[entry.Name()] = info.Mode()
	}
	if len(modes) != 2 || modes["fullchain.pem"] != 0o644 || modes["privkey.pem"] != 0o600 {
		t.Fatalf("lineage files: %v", modes)
	}
	if left := f.stagedLeftovers(t); len(left) != 0 {
		t.Fatalf("left beside the lineage: %v", left)
	}
	// A name and its wildcard are proved by two records under one name, given one wait.
	if len(f.dns.created) != 2 || !slices.Equal(f.dns.created, f.dns.deleted) || f.dns.remaining() != 0 {
		t.Fatalf("challenge records: created %v, deleted %v", f.dns.created, f.dns.deleted)
	}
	if !slices.Equal(f.slept, []time.Duration{30 * time.Second}) {
		t.Fatalf("propagation waits: %v", f.slept)
	}
	// No credential is written to the ACME state.
	_ = filepath.WalkDir(f.acmeDir, func(path string, _ os.DirEntry, _ error) error {
		if data, err := os.ReadFile(path); err == nil && bytes.Contains(data, []byte("synthetic")) {
			t.Fatalf("%s holds the DNS token", path)
		}
		return nil
	})
}

func TestIssuanceRegistersANewAccountOnce(t *testing.T) {
	f := newIssuance(t)
	if _, _, err := f.r.issueCertificate(t.Context(), issuanceSpec); err != nil {
		t.Fatal(err)
	}
	registrations := f.authority.registrations
	if len(registrations) != 1 || !slices.Equal(registrations[0].contact, []string{"mailto:ops@example.test"}) || !registrations[0].terms {
		t.Fatalf("registrations: %+v", registrations)
	}
	path := accountKeyPath(f.acmeDir, f.authority.directory())
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("account key: %v %v", info, err)
	}
	data, _ := os.ReadFile(path)
	key, err := firstPrivateKey(data)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := key.(*ecdsa.PrivateKey); !ok {
		t.Fatalf("account key is %T", key)
	}
	thumbprint, _ := acme.JWKThumbprint(key.Public())
	if registrations[0].thumbprint != thumbprint {
		t.Fatal("the account registered is not the key saved")
	}
	// The next issuance signs in with the saved key: no second account.
	if _, _, err := f.r.issueCertificate(t.Context(), issuanceSpec); err != nil {
		t.Fatal(err)
	}
	if len(f.authority.registrations) != 1 || f.authority.lookups[len(f.authority.lookups)-1] != thumbprint {
		t.Fatalf("second issuance registered again: %+v", f.authority.registrations)
	}
}

// rsaJWKJSON is a private RSA key as certbot's private_key.json holds it.
func rsaJWKJSON(key *rsa.PrivateKey) []byte {
	encode := func(n *big.Int) string { return base64.RawURLEncoding.EncodeToString(n.Bytes()) }
	key.Precompute()
	data, _ := json.Marshal(map[string]string{
		"kty": "RSA", "n": encode(key.N), "e": encode(big.NewInt(int64(key.E))), "d": encode(key.D),
		"p": encode(key.Primes[0]), "q": encode(key.Primes[1]),
		"dp": encode(key.Precomputed.Dp), "dq": encode(key.Precomputed.Dq), "qi": encode(key.Precomputed.Qinv),
	})
	return data
}

func TestIssuanceKeepsCertbotsAccount(t *testing.T) {
	f := newIssuance(t)
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	directory, _ := url.Parse(f.authority.directory())
	accountDir := filepath.Join(f.acmeDir, "config", "accounts", directory.Host, "directory", "3f2a9c")
	_ = os.MkdirAll(accountDir, 0o700)
	if err := os.WriteFile(filepath.Join(accountDir, "private_key.json"), rsaJWKJSON(key), 0o400); err != nil {
		t.Fatal(err)
	}
	thumbprint, _ := acme.JWKThumbprint(&key.PublicKey)
	f.authority.knownAccount(thumbprint)

	if _, _, err := f.r.issueCertificate(t.Context(), issuanceSpec); err != nil {
		t.Fatal(err)
	}
	if len(f.authority.registrations) != 0 || !slices.Contains(f.authority.lookups, thumbprint) {
		t.Fatalf("certbot's account was not the one used: registrations %+v", f.authority.registrations)
	}
	data, _ := os.ReadFile(accountKeyPath(f.acmeDir, f.authority.directory()))
	saved, err := firstPrivateKey(data)
	if err != nil || !key.PublicKey.Equal(saved.Public()) {
		t.Fatalf("the controller did not keep certbot's key: %v", err)
	}
}

func TestCertbotsAccountIsUsedOnlyWhenThereIsOne(t *testing.T) {
	dir := t.TempDir()
	directory := "https://acme.example/directory"
	if key, err := certbotAccountKey(dir, directory); key != nil || err != nil {
		t.Fatalf("none: %v %v", key, err)
	}
	for _, id := range []string{"one", "two"} {
		path := filepath.Join(dir, "config", "accounts", "acme.example", "directory", id)
		_ = os.MkdirAll(path, 0o700)
		_ = os.WriteFile(filepath.Join(path, "private_key.json"), []byte(`{"kty":"RSA"}`), 0o600)
	}
	if _, err := certbotAccountKey(dir, directory); err == nil || !strings.Contains(err.Error(), "certbot left 2 accounts") {
		t.Fatalf("several: %v", err)
	}
	if got := certbotAccountDir(dir, "https://acme.example/../../../outside"); got != "" {
		t.Fatalf("a directory address named a path outside the accounts: %s", got)
	}
}

func TestParseJWK(t *testing.T) {
	ec, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	raw, _ := ec.Bytes()
	public, _ := ec.PublicKey.Bytes()
	encode := base64.RawURLEncoding.EncodeToString
	data, _ := json.Marshal(map[string]string{"kty": "EC", "crv": "P-256", "d": encode(raw), "x": encode(public[1:33]), "y": encode(public[33:])})
	key, err := parseJWK(data)
	if err != nil || !ec.PublicKey.Equal(key.Public()) {
		t.Fatalf("EC: %v", err)
	}
	other, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	otherPublic, _ := other.PublicKey.Bytes()
	mismatched, _ := json.Marshal(map[string]string{"kty": "EC", "crv": "P-256", "d": encode(raw), "x": encode(otherPublic[1:33]), "y": encode(otherPublic[33:])})
	for name, data := range map[string][]byte{
		"mismatched point": mismatched,
		"unknown type":     []byte(`{"kty":"oct","k":"AAAA"}`),
		"broken RSA":       []byte(`{"kty":"RSA","n":"AQAB","e":"AQAB","d":"AQAB","p":"Aw","q":"Bw"}`),
		"not JSON":         []byte(`not json`),
	} {
		if _, err := parseJWK(data); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	rsaKey, _ := rsa.GenerateKey(rand.Reader, 2048)
	if key, err := parseJWK(rsaJWKJSON(rsaKey)); err != nil || !rsaKey.PublicKey.Equal(key.Public()) {
		t.Fatalf("RSA: %v", err)
	}
}

// A failure at any step leaves the lineage that was there, or none, and no
// challenge record behind.
func TestFailedIssuanceLeavesNoPartialLineage(t *testing.T) {
	cases := []struct {
		name    string
		prepare func(*issuance)
		want    string
		records int
	}{
		{"order refused", func(f *issuance) { f.authority.fail = "order" }, "the certificate authority refused the order", 0},
		{"record refused", func(f *issuance) { f.dns.refuseCreate = 1 }, "record refused", 1},
		{"challenge failed", func(f *issuance) { f.authority.fail = "challenge" }, "could not confirm control of the name", 2},
		{"challenge not answered", func(f *issuance) { f.authority.dns = &dnsFake{records: map[string][2]string{}} }, "could not confirm control of the name", 2},
		{"finalize refused", func(f *issuance) { f.authority.fail = "finalize" }, "did not issue the certificate", 2},
	}
	for _, c := range cases {
		for _, existing := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/existing=%v", c.name, existing), func(t *testing.T) {
				f := newIssuance(t)
				oldChain, oldKey, _ := newTestPKI(t).leaf(2, tlsNow.AddDate(0, 0, 5), "a.example", "*.a.example")
				if existing {
					f.certbotLineage(t, oldChain, oldKey)
				}
				c.prepare(f)
				_, _, err := f.r.issueCertificate(t.Context(), issuanceSpec)
				if err == nil || !strings.Contains(err.Error(), c.want) {
					t.Fatalf("error: %v", err)
				}
				if len(f.dns.created) != c.records || !slices.Equal(f.dns.created, f.dns.deleted) || f.dns.remaining() != 0 {
					t.Fatalf("challenge records: created %v, deleted %v", f.dns.created, f.dns.deleted)
				}
				chain, key, readErr := readLineage(f.lineageDir(t))
				if existing && (readErr != nil || !bytes.Equal(chain, oldChain) || !bytes.Equal(key, oldKey)) {
					t.Fatalf("the previous lineage changed: %v", readErr)
				}
				if !existing && !os.IsNotExist(readErr) {
					t.Fatalf("a lineage appeared: %v", readErr)
				}
				if left := f.stagedLeftovers(t); len(left) != 0 {
					t.Fatalf("left beside the lineage: %v", left)
				}
			})
		}
	}
}

// A certificate that does not cover the spec's names is never saved.
func TestIssuedCertificateIsCheckedBeforeItIsSaved(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "live", "a")
	pki := newTestPKI(t)
	chain, key, _ := pki.leaf(2, tlsNow.AddDate(0, 3, 0), "a.example")
	if err := saveIssued(dir, issued{fullchain: chain, privateKey: key}, []string{"a.example", "b.example"}); err == nil {
		t.Fatal("a certificate short of a name was saved")
	}
	_, other, _ := pki.leaf(3, tlsNow.AddDate(0, 3, 0), "a.example")
	if err := saveIssued(dir, issued{fullchain: chain, privateKey: other}, []string{"a.example"}); err == nil {
		t.Fatal("a certificate with another key was saved")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("a lineage appeared: %v", err)
	}
}

// When the certificate was issued but a record could not be removed, the
// certificate is kept and the failure is still reported: the next renewal
// resumes from the saved lineage.
func TestIssuanceKeepsTheCertificateWhenCleanupFails(t *testing.T) {
	f := newIssuance(t)
	f.dns.refuseDelete = true
	_, _, err := f.r.issueCertificate(t.Context(), issuanceSpec)
	if err == nil || !strings.Contains(err.Error(), "the challenge record _acme-challenge.a.example could not be removed") {
		t.Fatalf("error: %v", err)
	}
	chain, key, readErr := readLineage(f.lineageDir(t))
	if readErr != nil {
		t.Fatal(readErr)
	}
	if _, err := validateCertificate(chain, key, issuanceSpec.Domains); err != nil {
		t.Fatal(err)
	}
}

func TestIssuanceRefusesAnUnreadablePropagationSetting(t *testing.T) {
	for _, value := range []string{"soon", "-1", "3601"} {
		f := newIssuance(t)
		f.r.Env.ACMEPropagationSeconds = value
		if _, _, err := f.r.issueCertificate(t.Context(), issuanceSpec); err == nil || !strings.Contains(err.Error(), "ACME_PROPAGATION_SECONDS") {
			t.Fatalf("%q: %v", value, err)
		}
		if len(f.authority.lookups)+len(f.dns.created) != 0 {
			t.Fatalf("%q: something was asked before the setting was read", value)
		}
	}
	f := newIssuance(t)
	f.r.Env.ACMEPropagationSeconds = "0"
	if _, _, err := f.r.issueCertificate(t.Context(), issuanceSpec); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(f.slept, []time.Duration{0}) {
		t.Fatalf("waits: %v", f.slept)
	}
}

// State the controller cannot write stops issuance before the authority is asked.
func TestIssuanceRefusesUnwritableACMEState(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root writes anywhere")
	}
	f := newIssuance(t)
	_ = os.Chmod(f.acmeDir, 0o500)
	defer os.Chmod(f.acmeDir, 0o700)
	if _, _, err := f.r.issueCertificate(t.Context(), issuanceSpec); err == nil || err.Error() != "ACME state directory is not writable" {
		t.Fatalf("%v", err)
	}
	if len(f.authority.lookups) != 0 {
		t.Fatal("the authority was asked")
	}
}
