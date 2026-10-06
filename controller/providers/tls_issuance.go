package providers

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

var certificateEnd = []byte("-----END CERTIFICATE-----")

// splitChain is the leaf with its end marker, and the rest of the chain.
func splitChain(fullchain []byte) ([]byte, []byte, error) {
	leaf, chain, found := bytes.Cut(fullchain, certificateEnd)
	if !found {
		return nil, nil, &ProviderError{Message: "certificate chain does not contain a leaf certificate"}
	}
	out := append(append(append([]byte{}, leaf...), certificateEnd...), '\n')
	return out, bytes.TrimLeft(chain, " \t\n\r\x0b\x0c"), nil
}

// certificateBundle is the tar a Caddy consumer's deploy command receives.
func certificateBundle(fullchain, privateKey []byte) []byte {
	var buffer bytes.Buffer
	archive := tar.NewWriter(&buffer)
	for _, file := range []struct {
		name  string
		value []byte
	}{{"fullchain.pem", fullchain}, {"privkey.pem", privateKey}} {
		_ = archive.WriteHeader(&tar.Header{Name: file.name, Mode: 0o600, Size: int64(len(file.value)), Typeflag: tar.TypeReg, ModTime: time.Unix(0, 0), Format: tar.FormatUSTAR})
		_, _ = archive.Write(file.value)
	}
	_ = archive.Close()
	return buffer.Bytes()
}

// readBundle reads a snapshot bundle: exactly the two files, nothing else.
func readBundle(payload []byte) ([]byte, []byte, error) {
	reader := tar.NewReader(bytes.NewReader(payload))
	names := map[string]bool{}
	files := map[string][]byte{}
	regular := map[string]bool{}
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, nil, &ProviderError{Message: "certificate snapshot was invalid"}
		}
		names[header.Name] = true
		regular[header.Name] = header.Typeflag == tar.TypeReg
		if header.Typeflag == tar.TypeReg {
			data, err := io.ReadAll(reader)
			if err != nil {
				return nil, nil, &ProviderError{Message: "certificate snapshot was invalid"}
			}
			files[header.Name] = data
		}
	}
	if len(names) == 0 {
		return nil, nil, &ProviderError{Message: "certificate snapshot was invalid"}
	}
	if len(names) != 2 || !names["fullchain.pem"] || !names["privkey.pem"] {
		return nil, nil, &ProviderError{Message: "certificate snapshot contained unexpected files"}
	}
	if !regular["fullchain.pem"] || !regular["privkey.pem"] {
		return nil, nil, &ProviderError{Message: "certificate snapshot was incomplete"}
	}
	return files["fullchain.pem"], files["privkey.pem"], nil
}

// firstCertificate is the first certificate block in data.
func firstCertificate(data []byte) (*x509.Certificate, error) {
	for {
		block, rest := pem.Decode(data)
		if block == nil {
			return nil, errors.New("no certificate")
		}
		if block.Type == "CERTIFICATE" || block.Type == "X509 CERTIFICATE" {
			return x509.ParseCertificate(block.Bytes)
		}
		data = rest
	}
}

// firstPrivateKey is the first private key block in data.
func firstPrivateKey(data []byte) (crypto.Signer, error) {
	for {
		block, rest := pem.Decode(data)
		if block == nil {
			return nil, errors.New("no private key")
		}
		switch block.Type {
		case "PRIVATE KEY":
			key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
			if err != nil {
				return nil, err
			}
			signer, ok := key.(crypto.Signer)
			if !ok {
				return nil, errors.New("unsupported key")
			}
			return signer, nil
		case "RSA PRIVATE KEY":
			return x509.ParsePKCS1PrivateKey(block.Bytes)
		case "EC PRIVATE KEY":
			return x509.ParseECPrivateKey(block.Bytes)
		case "ENCRYPTED PRIVATE KEY":
			return nil, errors.New("encrypted key")
		}
		data = rest
	}
}

// validateCertificate checks the chain's leaf against its key and the names it
// must cover, and returns the leaf's SHA-256 fingerprint.
func validateCertificate(fullchain, privateKey []byte, domains []string) (string, error) {
	leaf, err := firstCertificate(fullchain)
	if err != nil {
		return "", &ProviderError{Message: "certificate unreadable", Err: err}
	}
	key, err := firstPrivateKey(privateKey)
	if err != nil {
		return "", &ProviderError{Message: "private key unreadable", Err: err}
	}
	certificatePublic, err := x509.MarshalPKIXPublicKey(leaf.PublicKey)
	if err != nil {
		return "", &ProviderError{Message: "certificate unreadable", Err: err}
	}
	keyPublic, err := x509.MarshalPKIXPublicKey(key.Public())
	if err != nil {
		return "", &ProviderError{Message: "private key unreadable", Err: err}
	}
	if !bytes.Equal(certificatePublic, keyPublic) {
		return "", &ProviderError{Message: "certificate and private key do not match"}
	}
	digest := sha256.Sum256(leaf.Raw)
	sans := nameSet(leaf.DNSNames)
	missing := []string{}
	for _, domain := range sortedKeys(nameSet(domains)) {
		if !sans[domain] {
			missing = append(missing, domain)
		}
	}
	if len(missing) > 0 {
		return "", &ProviderError{Message: "issued certificate is missing names: " + strings.Join(missing, ", ")}
	}
	return hex.EncodeToString(digest[:]), nil
}

func (r *Registry) acmeDir() (string, error) {
	if r.Env.ACMEDir == "" {
		return "", &ProviderError{Err: runtime.ErrSettingMissing}
	}
	return r.Env.ACMEDir, nil
}

// certificateName and certificateDomain are the contract's patterns, which
// HQ's tls.certificate declaration takes its name pattern from, checked again
// where they reach certbot's argv and a path.
var (
	certificateName       = api.MustPattern("TLSCertificateName")
	certificateNameLength = api.MustLimit("TLSCertificateName", "maxLength")
	certificateDomain     = api.MustPattern("TLSCertificateDomain")
)

// lineagePath is where certbot keeps this certificate's lineage, for a name that is one.
func (r *Registry) lineagePath(spec TLSCertificateSpec) (string, error) {
	if len([]rune(spec.CertificateName)) > certificateNameLength || !certificateName.MatchString(spec.CertificateName) {
		return "", &ProviderError{Message: "the certificate name is not a lineage name"}
	}
	acme, err := r.acmeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(acme, "config", "live", spec.CertificateName), nil
}

// checkedDomains is the names certbot is asked for, each a domain name, never an option.
func checkedDomains(spec TLSCertificateSpec) ([]string, error) {
	for _, domain := range spec.Domains {
		if !certificateDomain.MatchString(domain) {
			return nil, &ProviderError{Message: "a certificate domain is not a domain name"}
		}
	}
	return spec.Domains, nil
}

func readLineage(dir string) ([]byte, []byte, error) {
	fullchain, err := os.ReadFile(filepath.Join(dir, "fullchain.pem"))
	if err != nil {
		return nil, nil, err
	}
	privateKey, err := os.ReadFile(filepath.Join(dir, "privkey.pem"))
	if err != nil {
		return nil, nil, err
	}
	return fullchain, privateKey, nil
}

// lineage is the certificate certbot last saved for this spec.
func (r *Registry) lineage(spec TLSCertificateSpec) ([]byte, []byte, error) {
	dir, err := r.lineagePath(spec)
	if err != nil {
		return nil, nil, err
	}
	fullchain, privateKey, err := readLineage(dir)
	if err != nil {
		return nil, nil, &ProviderError{Message: "certbot lineage is unavailable for reconciliation"}
	}
	return fullchain, privateKey, nil
}

// lineageMaterial reads the lineage only when called, so an unchanged pass never opens the key.
func (r *Registry) lineageMaterial(spec TLSCertificateSpec) func() ([]byte, []byte, error) {
	return func() ([]byte, []byte, error) {
		dir, err := r.lineagePath(spec)
		if err != nil {
			return nil, nil, err
		}
		return readLineage(dir)
	}
}

// resumableLineage reuses a newer artifact a failed transaction left, instead of issuing again.
func (r *Registry) resumableLineage(spec TLSCertificateSpec, deployedFingerprint string) ([]byte, []byte, bool, error) {
	dir, err := r.lineagePath(spec)
	if err != nil {
		return nil, nil, false, err
	}
	fullchain, privateKey, err := readLineage(dir)
	if err != nil {
		return nil, nil, false, nil
	}
	fingerprint, err := validateCertificate(fullchain, privateKey, spec.Domains)
	if err != nil {
		return nil, nil, false, err
	}
	if fingerprint == deployedFingerprint {
		return nil, nil, false, nil
	}
	leaf, err := firstCertificate(fullchain)
	if err != nil {
		return nil, nil, false, &ProviderError{Message: "lineage certificate unreadable", Err: err}
	}
	minimum := r.Now().UTC().Add(time.Duration(spec.RenewalWindowDays) * 24 * time.Hour)
	if !leaf.NotAfter.UTC().Truncate(time.Second).After(minimum) {
		return nil, nil, false, nil
	}
	return fullchain, privateKey, true, nil
}

// foreignACMEEntry is the first ACME state entry this process could not take
// ownership of: certbot copies a key's owner onto its renewal, and a process
// that is not root can only chown to itself.
func foreignACMEEntry(acmeDir string) string {
	uid, gid := os.Getuid(), os.Getgid()
	var walk func(root string) string
	walk = func(root string) string {
		entries, err := os.ReadDir(root)
		if err != nil {
			return ""
		}
		dirs, files := []string{}, []string{}
		for _, entry := range entries {
			if entry.IsDir() {
				dirs = append(dirs, entry.Name())
			} else {
				files = append(files, entry.Name())
			}
		}
		slices.Sort(dirs)
		slices.Sort(files)
		for _, name := range append(append([]string{}, dirs...), files...) {
			path := filepath.Join(root, name)
			info, err := os.Lstat(path)
			if err != nil {
				continue
			}
			stat, ok := info.Sys().(*syscall.Stat_t)
			if !ok {
				continue
			}
			if int(stat.Uid) != uid || int(stat.Gid) != gid {
				relative, _ := filepath.Rel(acmeDir, path)
				return fmt.Sprintf("%s is owned %d:%d, not %d:%d", relative, stat.Uid, stat.Gid, uid, gid)
			}
		}
		for _, name := range dirs {
			info, err := os.Lstat(filepath.Join(root, name))
			if err != nil || info.Mode()&os.ModeSymlink != 0 {
				continue
			}
			if found := walk(filepath.Join(root, name)); found != "" {
				return found
			}
		}
		return ""
	}
	return walk(acmeDir)
}

// writePrivate writes a credential readable by this account alone from its first byte.
func writePrivate(path, text string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	_, writeErr := file.WriteString(text)
	closeErr := file.Close()
	if writeErr != nil {
		return writeErr
	}
	return closeErr
}

// cloudflareToken is the DNS-edit token the ACME DNS-01 challenge uses.
func (r *Registry) cloudflareToken() (string, error) {
	_, token, err := r.cloudflareCredential(runtime.ConnectionProviderCloudflareDNS, "")
	return token.APIToken, err
}

// issueCertificate runs certbot's DNS-01 issuance and returns the new lineage.
func (r *Registry) issueCertificate(ctx context.Context, spec TLSCertificateSpec) ([]byte, []byte, error) {
	lineage, err := r.lineagePath(spec)
	if err != nil {
		return nil, nil, err
	}
	domains, err := checkedDomains(spec)
	if err != nil {
		return nil, nil, err
	}
	acme, err := r.acmeDir()
	if err != nil {
		return nil, nil, err
	}
	info, err := os.Stat(acme)
	if err != nil || !info.IsDir() || syscall.Access(acme, 2) != nil {
		return nil, nil, &ProviderError{Message: "ACME state directory is not writable"}
	}
	if foreign := foreignACMEEntry(acme); foreign != "" {
		return nil, nil, &ProviderError{Message: "ACME state is not wholly the controller's: " + foreign + ". Certbot would be issued a certificate it cannot save, so nothing was requested."}
	}
	if _, err := r.commands().Run(ctx, []string{"certbot", "--version"}, nil, "certbot preflight", "", nil); err != nil {
		return nil, nil, err
	}
	token, err := r.cloudflareToken()
	if err != nil {
		return nil, nil, err
	}
	credentials := filepath.Join(acme, "cloudflare.ini")
	if err := writePrivate(credentials, "dns_cloudflare_api_token = "+token+"\n"); err != nil {
		return nil, nil, &ProviderError{Message: "ACME credentials could not be written"}
	}
	defer os.Remove(credentials)
	// The certificate authority account is the one connection of its shape.
	account, err := runtime.Only(r.Supplied, func(c connections.Connection) *connections.ACME { return c.ACME })
	if err != nil {
		return nil, nil, err
	}
	email, directory := account.Email, account.DirectoryURL
	propagation := r.Env.ACMEPropagationSeconds
	if propagation == "" {
		propagation = "30"
	}
	argv := []string{
		"certbot", "certonly", "--non-interactive", "--agree-tos",
		"--email", email,
		"--server", directory,
		"--dns-cloudflare",
		"--dns-cloudflare-credentials", credentials,
		"--dns-cloudflare-propagation-seconds", propagation,
		"--config-dir", filepath.Join(acme, "config"),
		"--work-dir", filepath.Join(acme, "work"),
		"--logs-dir", filepath.Join(acme, "logs"),
		"--cert-name", spec.CertificateName,
		"--force-renewal",
	}
	for _, domain := range domains {
		argv = append(argv, "-d", domain)
	}
	if _, err := r.commands().Run(ctx, argv, nil, "certbot certonly", "", nil); err != nil {
		return nil, nil, err
	}
	fullchain, privateKey, err := readLineage(lineage)
	if err != nil {
		return nil, nil, &ProviderError{Message: "certbot did not produce a complete lineage", Err: err}
	}
	return fullchain, privateKey, nil
}
