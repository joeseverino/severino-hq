package providers

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/acme"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// DNS-01 issuance, spoken to the certificate authority directly. The account
// key, the lineage and nothing else are kept in the ACME state directory; the
// DNS token is used for the challenge records and never written down.

// acmeOrderTimeout bounds one issuance, from signing in to the saved lineage:
// an authorization the authority never decides must not hold the pass.
const acmeOrderTimeout = 10 * time.Minute

// acmeChallengeCleanup bounds removing the challenge records once the order
// has ended, which happens even when the order's own context was cancelled.
const acmeChallengeCleanup = time.Minute

// acmeHTTP is how the controller reaches the certificate authority: the
// controller's trust roots, its request timeout, and no redirect.
func (r *Registry) acmeHTTP() (*http.Client, error) {
	if r.ACMEHTTP != nil {
		return r.ACMEHTTP, nil
	}
	client, err := runtime.NewHTTPClient(r.Env.CAFile)
	if err != nil {
		return nil, err
	}
	return &http.Client{Transport: client.Transport, Timeout: client.Timeout, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}, nil
}

// propagationDelay is how long a challenge record is given to reach
// Cloudflare's nameservers before the certificate authority is asked to look.
func (r *Registry) propagationDelay() (time.Duration, error) {
	configured := r.Env.ACMEPropagationSeconds
	if configured == "" {
		configured = "30"
	}
	seconds, err := strconv.Atoi(configured)
	if err != nil || seconds < 0 || seconds > 3600 {
		return 0, &ProviderError{Message: "ACME_PROPAGATION_SECONDS is not a number of seconds between 0 and 3600"}
	}
	return time.Duration(seconds) * time.Second, nil
}

// accountKeyPath is where this controller keeps its key for one directory:
// one account per certificate authority, named by a digest of its address so
// no part of the address becomes a path.
func accountKeyPath(acmeDir, directory string) string {
	digest := sha256.Sum256([]byte(directory))
	return filepath.Join(acmeDir, "account", hex.EncodeToString(digest[:8])+".pem")
}

// certbotAccountDir is where certbot kept its accounts for a directory:
// config/accounts/<host><path>/<account id>/. Empty when the address would
// name a path outside that tree.
func certbotAccountDir(acmeDir, directory string) string {
	parsed, err := url.Parse(directory)
	if err != nil || parsed.Host == "" {
		return ""
	}
	root := filepath.Join(acmeDir, "config", "accounts")
	dir := filepath.Join(root, parsed.Host+parsed.Path)
	if relative, err := filepath.Rel(root, dir); err != nil || relative == "." || strings.HasPrefix(relative, "..") {
		return ""
	}
	return dir
}

// certbotAccountKey is the key of the account certbot registered with this
// directory, so the certificate authority keeps seeing the same account. Nil
// when certbot never registered one; an error when it registered several,
// because which one HQ's certificates belong to is the operator's call.
func certbotAccountKey(acmeDir, directory string) (crypto.Signer, error) {
	dir := certbotAccountDir(acmeDir, directory)
	if dir == "" {
		return nil, nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, nil
	}
	found := []string{}
	for _, entry := range entries {
		path := filepath.Join(dir, entry.Name(), "private_key.json")
		if info, err := os.Lstat(path); err == nil && info.Mode().IsRegular() {
			found = append(found, path)
		}
	}
	slices.Sort(found)
	switch len(found) {
	case 0:
		return nil, nil
	case 1:
	default:
		return nil, &ProviderError{Message: fmt.Sprintf("certbot left %d accounts for %s; keep the one HQ's certificates belong to and remove the others", len(found), directory)}
	}
	data, err := os.ReadFile(found[0])
	if err != nil {
		return nil, &ProviderError{Message: "certbot's account key could not be read", Err: err}
	}
	key, err := parseJWK(data)
	if err != nil {
		return nil, &ProviderError{Message: "certbot's account key is not a private key HQ can use", Err: err}
	}
	return key, nil
}

// jsonWebKey is the members of a private JWK (RFC 7517, 7518) certbot writes.
type jsonWebKey struct {
	Kty string `json:"kty"`
	N   string `json:"n"`
	E   string `json:"e"`
	D   string `json:"d"`
	P   string `json:"p"`
	Q   string `json:"q"`
	Crv string `json:"crv"`
	X   string `json:"x"`
	Y   string `json:"y"`
}

func jwkNumber(value string) (*big.Int, error) {
	data, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(value, "="))
	if err != nil || len(data) == 0 {
		return nil, errors.New("a key member is not base64url")
	}
	return new(big.Int).SetBytes(data), nil
}

// parseJWK reads an RSA or EC private key in JWK form.
func parseJWK(data []byte) (crypto.Signer, error) {
	var jwk jsonWebKey
	if err := json.Unmarshal(data, &jwk); err != nil {
		return nil, err
	}
	switch jwk.Kty {
	case "RSA":
		return rsaJWK(jwk)
	case "EC":
		return ecJWK(jwk)
	}
	return nil, fmt.Errorf("key type %q", jwk.Kty)
}

func rsaJWK(jwk jsonWebKey) (crypto.Signer, error) {
	numbers := map[string]*big.Int{}
	for name, value := range map[string]string{"n": jwk.N, "e": jwk.E, "d": jwk.D, "p": jwk.P, "q": jwk.Q} {
		number, err := jwkNumber(value)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", name, err)
		}
		numbers[name] = number
	}
	if !numbers["e"].IsInt64() || numbers["e"].Int64() > 1<<31-1 {
		return nil, errors.New("exponent out of range")
	}
	key := &rsa.PrivateKey{
		PublicKey: rsa.PublicKey{N: numbers["n"], E: int(numbers["e"].Int64())},
		D:         numbers["d"],
		Primes:    []*big.Int{numbers["p"], numbers["q"]},
	}
	if err := key.Validate(); err != nil {
		return nil, err
	}
	key.Precompute()
	return key, nil
}

func ecJWK(jwk jsonWebKey) (crypto.Signer, error) {
	curves := map[string]elliptic.Curve{"P-256": elliptic.P256(), "P-384": elliptic.P384(), "P-521": elliptic.P521()}
	curve, ok := curves[jwk.Crv]
	if !ok {
		return nil, fmt.Errorf("curve %q", jwk.Crv)
	}
	d, err := jwkNumber(jwk.D)
	if err != nil {
		return nil, err
	}
	size := (curve.Params().BitSize + 7) / 8
	if len(d.Bytes()) > size {
		return nil, errors.New("private scalar out of range")
	}
	key, err := ecdsa.ParseRawPrivateKey(curve, d.FillBytes(make([]byte, size)))
	if err != nil {
		return nil, err
	}
	x, xErr := jwkNumber(jwk.X)
	y, yErr := jwkNumber(jwk.Y)
	if xErr != nil || yErr != nil {
		return nil, errors.New("public point unreadable")
	}
	public, err := key.PublicKey.Bytes()
	if err != nil {
		return nil, err
	}
	expected := append(append([]byte{4}, x.FillBytes(make([]byte, size))...), y.FillBytes(make([]byte, size))...)
	if string(public) != string(expected) {
		return nil, errors.New("public point does not match the private scalar")
	}
	return key, nil
}

// writeFileAtomic replaces path with data readable by mode, so a reader sees
// the old file or the new one, never part of either.
func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	file, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	staged := file.Name()
	defer os.Remove(staged)
	if err := file.Chmod(mode); err != nil {
		file.Close()
		return err
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(staged, path); err != nil {
		return err
	}
	return syncDir(filepath.Dir(path))
}

func syncDir(dir string) error {
	handle, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer handle.Close()
	return handle.Sync()
}

func privateKeyPEM(key crypto.Signer) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

// accountKey is the key the controller signs to the certificate authority
// with: its own, else certbot's carried over, else a new one. Whichever it is
// is kept in the controller's own place before it is used.
func accountKey(acmeDir, directory string) (crypto.Signer, error) {
	path := accountKeyPath(acmeDir, directory)
	if data, err := os.ReadFile(path); err == nil {
		key, err := firstPrivateKey(data)
		if err != nil {
			return nil, &ProviderError{Message: "the ACME account key is unreadable", Err: err}
		}
		return key, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, &ProviderError{Message: "the ACME account key could not be read", Err: err}
	}
	key, err := certbotAccountKey(acmeDir, directory)
	if err != nil {
		return nil, err
	}
	if key == nil {
		if key, err = ecdsa.GenerateKey(elliptic.P256(), rand.Reader); err != nil {
			return nil, err
		}
	}
	encoded, err := privateKeyPEM(key)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, &ProviderError{Message: "the ACME account key could not be saved", Err: err}
	}
	if err := writeFileAtomic(path, encoded, 0o600); err != nil {
		return nil, &ProviderError{Message: "the ACME account key could not be saved", Err: err}
	}
	return key, nil
}

// acmeClient is a client signed in to the account the ACME connection names,
// registering it, with the terms agreed, the first time its key is seen.
func (r *Registry) acmeClient(ctx context.Context, acmeDir string) (*acme.Client, error) {
	// The certificate authority account is the one connection of its shape.
	account, err := runtime.Only(r.Supplied, func(c connections.Connection) *connections.ACME { return c.ACME })
	if err != nil {
		return nil, err
	}
	key, err := accountKey(acmeDir, account.DirectoryURL)
	if err != nil {
		return nil, err
	}
	httpClient, err := r.acmeHTTP()
	if err != nil {
		return nil, err
	}
	client := &acme.Client{Key: key, DirectoryURL: account.DirectoryURL, HTTPClient: httpClient, UserAgent: "severino-hq-controller"}
	if _, err := client.GetReg(ctx, ""); err == nil {
		return client, nil
	} else if !errors.Is(err, acme.ErrNoAccount) {
		return nil, &ProviderError{Message: "the certificate authority did not answer for the account", Err: err}
	}
	registration := &acme.Account{Contact: []string{"mailto:" + account.Email}}
	if _, err := client.Register(ctx, registration, acme.AcceptTOS); err != nil && !errors.Is(err, acme.ErrAccountAlreadyExists) {
		return nil, &ProviderError{Message: "the certificate authority refused to register the account", Err: err}
	}
	return client, nil
}

// challengeRecord is one TXT record placed for one authorization.
type challengeRecord struct {
	name, zoneID, recordID string
}

// challengeZone is the Cloudflare zone a record name falls in: the longest
// zone name it ends with.
func (r *Registry) challengeZone(ctx context.Context, name string) (string, error) {
	zones, err := r.cloudflareZones(ctx)
	if err != nil {
		return "", err
	}
	best, bestID := "", ""
	for _, zone := range zones {
		candidate := hostname(zone.Name)
		if candidate != "" && strings.HasSuffix(name, "."+candidate) && len(candidate) > len(best) {
			best, bestID = candidate, zone.ID
		}
	}
	if bestID == "" {
		return "", &ProviderError{Message: fmt.Sprintf("the cloudflare credential cannot see a zone for %q", name)}
	}
	return bestID, nil
}

// placeChallenge creates the TXT record that answers one DNS-01 challenge.
func (r *Registry) placeChallenge(ctx context.Context, name, value string) (challengeRecord, error) {
	zoneID, err := r.challengeZone(ctx, name)
	if err != nil {
		return challengeRecord{}, err
	}
	payload, err := cloudflarePayload(CloudflareDNSRecordSpec{Name: name, RecordType: "TXT", Content: value, TTL: 60})
	if err != nil {
		return challengeRecord{}, err
	}
	written, err := writtenRecord(r.cloudflareRequest(ctx, "/zones/"+zoneID+"/dns_records", "POST", payload))
	if err != nil {
		return challengeRecord{}, err
	}
	if written.ID == "" {
		return challengeRecord{}, &ProviderError{Message: "cloudflare created the challenge record without an id"}
	}
	return challengeRecord{name: name, zoneID: zoneID, recordID: written.ID}, nil
}

// removeChallenges deletes every record placed, whether the order succeeded
// or not, on a context of its own so a cancelled order still cleans up.
func (r *Registry) removeChallenges(ctx context.Context, placed []challengeRecord) error {
	if len(placed) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), acmeChallengeCleanup)
	defer cancel()
	var failed []error
	for _, record := range placed {
		if _, err := r.cloudflareRequest(ctx, "/zones/"+record.zoneID+"/dns_records/"+record.recordID, "DELETE", nil); err != nil {
			failed = append(failed, fmt.Errorf("the challenge record %s could not be removed: %w", record.name, err))
		}
	}
	return errors.Join(failed...)
}

// pendingChallenge is an authorization the order still has to prove.
type pendingChallenge struct {
	authorization string
	challenge     *acme.Challenge
}

// authorize proves control of every name the order lists: it places each
// challenge record first, gives them one propagation wait together (a name
// and its wildcard share a record name), then asks for each to be checked.
// The records placed are returned even on failure, for removal.
func (r *Registry) authorize(ctx context.Context, client *acme.Client, order *acme.Order) ([]challengeRecord, error) {
	placed := []challengeRecord{}
	pending := []pendingChallenge{}
	for _, address := range order.AuthzURLs {
		authorization, err := client.GetAuthorization(ctx, address)
		if err != nil {
			return placed, &ProviderError{Message: "the certificate authority did not answer for an authorization", Err: err}
		}
		if authorization.Status == acme.StatusValid {
			continue
		}
		index := slices.IndexFunc(authorization.Challenges, func(c *acme.Challenge) bool { return c.Type == "dns-01" })
		if index < 0 {
			return placed, &ProviderError{Message: "the certificate authority offered no DNS-01 challenge for " + authorization.Identifier.Value}
		}
		challenge := authorization.Challenges[index]
		value, err := client.DNS01ChallengeRecord(challenge.Token)
		if err != nil {
			return placed, err
		}
		record, err := r.placeChallenge(ctx, "_acme-challenge."+hostname(authorization.Identifier.Value), value)
		if err != nil {
			return placed, err
		}
		placed = append(placed, record)
		pending = append(pending, pendingChallenge{authorization: address, challenge: challenge})
	}
	if len(pending) == 0 {
		return placed, nil
	}
	delay, err := r.propagationDelay()
	if err != nil {
		return placed, err
	}
	if err := r.Sleep(ctx, delay); err != nil {
		return placed, err
	}
	for _, item := range pending {
		if _, err := client.Accept(ctx, item.challenge); err != nil {
			return placed, &ProviderError{Message: "the certificate authority refused the challenge", Err: err}
		}
	}
	for _, item := range pending {
		if _, err := client.WaitAuthorization(ctx, item.authorization); err != nil {
			return placed, &ProviderError{Message: "the certificate authority could not confirm control of the name", Err: err}
		}
	}
	return placed, nil
}

// issued is what an order produced: the full chain and the fresh key it
// certifies, as PEM, and the challenge records left to remove.
type issued struct {
	fullchain, privateKey []byte
	placed                []challengeRecord
}

// orderCertificate runs one order to its certificate. The records it placed
// come back on failure too: removing them is the caller's, after the result
// is saved, so a failed removal never costs the certificate.
func (r *Registry) orderCertificate(ctx context.Context, client *acme.Client, domains []string) (issued, error) {
	order, err := client.AuthorizeOrder(ctx, acme.DomainIDs(domains...))
	if err != nil {
		return issued{}, &ProviderError{Message: "the certificate authority refused the order", Err: err}
	}
	placed, err := r.authorize(ctx, client, order)
	result := issued{placed: placed}
	if err != nil {
		return result, err
	}
	if order, err = client.WaitOrder(ctx, order.URI); err != nil {
		return result, &ProviderError{Message: "the order did not become ready", Err: err}
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return result, err
	}
	csr, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{DNSNames: domains}, key)
	if err != nil {
		return result, err
	}
	chain, _, err := client.CreateOrderCert(ctx, order.FinalizeURL, csr, true)
	if err != nil {
		return result, &ProviderError{Message: "the certificate authority did not issue the certificate", Err: err}
	}
	privateKey, err := privateKeyPEM(key)
	if err != nil {
		return result, err
	}
	for _, der := range chain {
		result.fullchain = append(result.fullchain, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})...)
	}
	result.privateKey = privateKey
	return result, nil
}
