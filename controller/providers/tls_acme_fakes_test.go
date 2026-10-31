package providers

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// acmeFake is a certificate authority speaking RFC 8555 over httptest, enough
// of it for one order at a time. It checks each DNS-01 answer against the
// records the DNS fake holds, and issues from a test PKI.
type acmeFake struct {
	t    *testing.T
	pki  *testPKI
	base string
	// dns is where challenge records are looked up; nil accepts every answer.
	dns *dnsFake
	// fail names the step that refuses: "order", "finalize", or "challenge".
	fail     string
	notAfter time.Time
	// issuedDER is the last leaf issued.
	issuedDER []byte

	mu            sync.Mutex
	accounts      map[string]string // JWK thumbprint -> account URL
	thumbprints   map[string]string // account URL -> JWK thumbprint
	registrations []acmeRegistration
	lookups       []string // thumbprints asked about with onlyReturnExisting
	identifiers   []string
	authorized    map[int]string // authorization index -> status
	certified     bool
}

type acmeRegistration struct {
	thumbprint string
	contact    []string
	terms      bool
}

func newACMEFake(t *testing.T, pki *testPKI, dns *dnsFake) *acmeFake {
	t.Helper()
	fake := &acmeFake{t: t, pki: pki, dns: dns, notAfter: tlsNow.AddDate(0, 3, 0), accounts: map[string]string{}, thumbprints: map[string]string{}}
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	fake.base = server.URL
	return fake
}

func (f *acmeFake) directory() string { return f.base + "/directory" }

func (f *acmeFake) problem(w http.ResponseWriter, status int, kind, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"type": "urn:ietf:params:acme:error:" + kind, "detail": detail, "status": status})
}

// jwkThumbprint is RFC 7638's thumbprint of a public JWK.
func jwkThumbprint(raw json.RawMessage) string {
	var key map[string]string
	_ = json.Unmarshal(raw, &key)
	var canonical string
	if key["kty"] == "RSA" {
		canonical = fmt.Sprintf(`{"e":%q,"kty":"RSA","n":%q}`, key["e"], key["n"])
	} else {
		canonical = fmt.Sprintf(`{"crv":%q,"kty":"EC","x":%q,"y":%q}`, key["crv"], key["x"], key["y"])
	}
	digest := sha256.Sum256([]byte(canonical))
	return base64.RawURLEncoding.EncodeToString(digest[:])
}

// signed is a request's JWS, read without checking the signature: the fake
// stands in for the authority's bookkeeping, not its cryptography.
type signed struct {
	thumbprint string
	account    string
	payload    []byte
}

func (f *acmeFake) read(r *http.Request) signed {
	var body struct{ Protected, Payload string }
	_ = json.NewDecoder(r.Body).Decode(&body)
	header, _ := base64.RawURLEncoding.DecodeString(body.Protected)
	payload, _ := base64.RawURLEncoding.DecodeString(body.Payload)
	var protected struct {
		JWK json.RawMessage `json:"jwk"`
		KID string          `json:"kid"`
	}
	_ = json.Unmarshal(header, &protected)
	f.mu.Lock()
	defer f.mu.Unlock()
	if protected.KID != "" {
		return signed{thumbprint: f.thumbprints[protected.KID], account: protected.KID, payload: payload}
	}
	return signed{thumbprint: jwkThumbprint(protected.JWK), payload: payload}
}

func (f *acmeFake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Replay-Nonce", "nonce-"+rand.Text())
	switch {
	case r.URL.Path == "/directory":
		writeJSON(w, http.StatusOK, map[string]any{
			"newNonce": f.base + "/nonce", "newAccount": f.base + "/account", "newOrder": f.base + "/order",
			"meta": map[string]any{"termsOfService": f.base + "/terms"},
		})
	case r.URL.Path == "/nonce":
		w.WriteHeader(http.StatusOK)
	case r.URL.Path == "/account":
		f.account(w, f.read(r))
	case r.URL.Path == "/order":
		f.newOrder(w, f.read(r))
	case strings.HasPrefix(r.URL.Path, "/authz/"):
		f.read(r)
		f.authorization(w, r.URL.Path)
	case strings.HasPrefix(r.URL.Path, "/chall/"):
		f.challenge(w, f.read(r), r.URL.Path)
	case r.URL.Path == "/order/1":
		f.read(r)
		f.order(w, http.StatusOK)
	case r.URL.Path == "/finalize/1":
		f.finalize(w, f.read(r))
	case r.URL.Path == "/cert/1":
		f.read(r)
		w.Header().Set("Content-Type", "application/pem-certificate-chain")
		_, _ = w.Write(append(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: f.issued()}), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: f.pki.caCert.Raw})...))
	default:
		f.problem(w, http.StatusNotFound, "malformed", "no such resource")
	}
}

func (f *acmeFake) account(w http.ResponseWriter, request signed) {
	var body struct {
		OnlyReturnExisting bool     `json:"onlyReturnExisting"`
		Contact            []string `json:"contact"`
		TermsAgreed        bool     `json:"termsOfServiceAgreed"`
	}
	_ = json.Unmarshal(request.payload, &body)
	f.mu.Lock()
	defer f.mu.Unlock()
	existing, found := f.accounts[request.thumbprint]
	if body.OnlyReturnExisting {
		f.lookups = append(f.lookups, request.thumbprint)
		if !found {
			f.problem(w, http.StatusBadRequest, "accountDoesNotExist", "no account for this key")
			return
		}
		w.Header().Set("Location", existing)
		writeJSON(w, http.StatusOK, map[string]any{"status": "valid"})
		return
	}
	if found {
		w.Header().Set("Location", existing)
		writeJSON(w, http.StatusOK, map[string]any{"status": "valid"})
		return
	}
	location := fmt.Sprintf("%s/acct/%d", f.base, len(f.accounts)+1)
	f.accounts[request.thumbprint] = location
	f.thumbprints[location] = request.thumbprint
	f.registrations = append(f.registrations, acmeRegistration{thumbprint: request.thumbprint, contact: body.Contact, terms: body.TermsAgreed})
	w.Header().Set("Location", location)
	writeJSON(w, http.StatusCreated, map[string]any{"status": "valid", "contact": body.Contact})
}

// issued is the leaf last issued, as DER.
func (f *acmeFake) issued() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.issuedDER
}

// knownAccount registers a key's thumbprint as an account the authority already holds.
func (f *acmeFake) knownAccount(thumbprint string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	location := fmt.Sprintf("%s/acct/%d", f.base, len(f.accounts)+1)
	f.accounts[thumbprint] = location
	f.thumbprints[location] = thumbprint
}

func (f *acmeFake) newOrder(w http.ResponseWriter, request signed) {
	if f.fail == "order" {
		f.problem(w, http.StatusForbidden, "rejectedIdentifier", "refused by policy")
		return
	}
	var body struct {
		Identifiers []struct{ Type, Value string } `json:"identifiers"`
	}
	_ = json.Unmarshal(request.payload, &body)
	f.mu.Lock()
	f.identifiers, f.authorized, f.certified = nil, map[int]string{}, false
	for i, identifier := range body.Identifiers {
		f.identifiers = append(f.identifiers, identifier.Value)
		f.authorized[i] = "pending"
	}
	f.mu.Unlock()
	w.Header().Set("Location", f.base+"/order/1")
	f.order(w, http.StatusCreated)
}

func (f *acmeFake) order(w http.ResponseWriter, status int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	state := "ready"
	authorizations := []string{}
	identifiers := []map[string]string{}
	for i, value := range f.identifiers {
		authorizations = append(authorizations, fmt.Sprintf("%s/authz/1/%d", f.base, i))
		identifiers = append(identifiers, map[string]string{"type": "dns", "value": value})
		switch f.authorized[i] {
		case "invalid":
			state = "invalid"
		case "pending":
			if state == "ready" {
				state = "pending"
			}
		}
	}
	body := map[string]any{"status": state, "identifiers": identifiers, "authorizations": authorizations, "finalize": f.base + "/finalize/1"}
	if f.certified {
		body["status"], body["certificate"] = "valid", f.base+"/cert/1"
	}
	writeJSON(w, status, body)
}

func (f *acmeFake) index(path string) int {
	var index int
	_, _ = fmt.Sscanf(path[strings.LastIndex(path, "/")+1:], "%d", &index)
	return index
}

func (f *acmeFake) authorization(w http.ResponseWriter, path string) {
	index := f.index(path)
	f.mu.Lock()
	defer f.mu.Unlock()
	value := f.identifiers[index]
	wildcard := strings.HasPrefix(value, "*.")
	status := f.authorized[index]
	writeJSON(w, http.StatusOK, map[string]any{
		"status": status, "identifier": map[string]string{"type": "dns", "value": strings.TrimPrefix(value, "*.")}, "wildcard": wildcard,
		"challenges": []map[string]any{
			{"type": "http-01", "url": fmt.Sprintf("%s/chall/1/http-%d", f.base, index), "token": "unused", "status": "pending"},
			{"type": "dns-01", "url": fmt.Sprintf("%s/chall/1/%d", f.base, index), "token": fmt.Sprintf("token-%d", index), "status": status},
		},
	})
}

// challenge checks the TXT record the controller placed, the way the
// authority would resolve it: the key authorization's digest under the name.
func (f *acmeFake) challenge(w http.ResponseWriter, request signed, path string) {
	index := f.index(path)
	f.mu.Lock()
	name := "_acme-challenge." + strings.TrimPrefix(f.identifiers[index], "*.")
	f.mu.Unlock()
	digest := sha256.Sum256(fmt.Appendf(nil, "token-%d.%s", index, request.thumbprint))
	want := base64.RawURLEncoding.EncodeToString(digest[:])
	ok := f.fail != "challenge" && (f.dns == nil || f.dns.holds(name, want))
	f.mu.Lock()
	f.authorized[index] = map[bool]string{true: "valid", false: "invalid"}[ok]
	status := f.authorized[index]
	f.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"type": "dns-01", "url": f.base + path, "token": fmt.Sprintf("token-%d", index), "status": status})
}

func (f *acmeFake) finalize(w http.ResponseWriter, request signed) {
	if f.fail == "finalize" {
		f.problem(w, http.StatusForbidden, "badCSR", "refused")
		return
	}
	var body struct {
		CSR string `json:"csr"`
	}
	_ = json.Unmarshal(request.payload, &body)
	der, _ := base64.RawURLEncoding.DecodeString(body.CSR)
	csr, err := x509.ParseCertificateRequest(der)
	if err != nil || csr.CheckSignature() != nil {
		f.problem(w, http.StatusBadRequest, "badCSR", "unreadable")
		return
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), DNSNames: csr.DNSNames, NotBefore: time.Unix(0, 0), NotAfter: f.notAfter}
	issued, err := x509.CreateCertificate(rand.Reader, template, f.pki.caCert, csr.PublicKey, f.pki.caKey)
	if err != nil {
		f.t.Error(err)
	}
	f.mu.Lock()
	f.issuedDER, f.certified = issued, true
	f.mu.Unlock()
	w.Header().Set("Location", f.base+"/order/1")
	f.order(w, http.StatusOK)
}

// dnsFake is Cloudflare's DNS record surface, with state: records created
// are held until deleted.
type dnsFake struct {
	mu      sync.Mutex
	next    int
	records map[string][2]string // id -> name, content
	created []string
	deleted []string
	// refuseCreate refuses record creation after this many succeed; -1 never.
	refuseCreate int
	refuseDelete bool
}

func newDNSFake(t *testing.T) (*dnsFake, string) {
	t.Helper()
	fake := &dnsFake{records: map[string][2]string{}, refuseCreate: -1}
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	return fake, server.URL
}

func (f *dnsFake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	recordVendorCall("cloudflare", r.Method, r.URL.Path)
	f.mu.Lock()
	defer f.mu.Unlock()
	ok := func(result any) {
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "errors": []any{}, "result": result})
	}
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/zones":
		ok([]map[string]any{{"id": "zone-outer", "name": "example"}, {"id": "zone-a", "name": "a.example"}})
	case r.Method == http.MethodPost && r.URL.Path == "/zones/zone-a/dns_records":
		if f.refuseCreate >= 0 && len(f.created) >= f.refuseCreate {
			writeJSON(w, http.StatusBadRequest, map[string]any{"success": false, "errors": []map[string]any{{"code": 81058, "message": "record refused"}}})
			return
		}
		var body struct{ Type, Name, Content string }
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.next++
		id := fmt.Sprintf("record%d", f.next)
		f.records[id] = [2]string{body.Name, body.Content}
		f.created = append(f.created, id)
		ok(map[string]any{"id": id, "type": body.Type, "name": body.Name, "content": body.Content, "ttl": 60})
	case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/zones/zone-a/dns_records/"):
		if f.refuseDelete {
			writeJSON(w, http.StatusBadRequest, map[string]any{"success": false, "errors": []map[string]any{{"code": 81044, "message": "record busy"}}})
			return
		}
		id := strings.TrimPrefix(r.URL.Path, "/zones/zone-a/dns_records/")
		delete(f.records, id)
		f.deleted = append(f.deleted, id)
		ok(map[string]any{"id": id})
	default:
		writeJSON(w, http.StatusNotFound, map[string]any{"success": false, "errors": []map[string]any{{"code": 7003, "message": "no route"}}})
	}
}

// holds is whether a TXT record with this name and value exists now.
func (f *dnsFake) holds(name, value string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, record := range f.records {
		if record[0] == name && strings.Trim(record[1], `"`) == value {
			return true
		}
	}
	return false
}

func (f *dnsFake) remaining() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.records)
}
