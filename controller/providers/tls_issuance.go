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
// where they reach an order, a challenge record name and a path.
var (
	certificateName       = api.MustPattern("TLSCertificateName")
	certificateNameLength = api.MustLimit("TLSCertificateName", "maxLength")
	certificateDomain     = api.MustPattern("TLSCertificateDomain")
)

// lineagePath is where this certificate's lineage is kept, for a name that is
// one: config/live/<name>, where certbot kept it, so a lineage it saved is read
// unchanged until the next issuance replaces it.
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

// checkedDomains is the names an order asks for, each a domain name.
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

// lineage is the certificate last saved for this spec.
func (r *Registry) lineage(spec TLSCertificateSpec) ([]byte, []byte, error) {
	dir, err := r.lineagePath(spec)
	if err != nil {
		return nil, nil, err
	}
	fullchain, privateKey, err := readLineage(dir)
	if err != nil {
		return nil, nil, &ProviderError{Message: "the certificate lineage is unavailable for reconciliation"}
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
// ownership of. Issuance replaces a lineage directory and the files in it, and
// a process that is not root cannot remove what another account owns there.
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

// saveLineage puts a certificate where readLineage finds it: the two files
// are written to a directory beside the lineage, then the two directories
// trade names in one step, so a reader sees the old pair or the new one.
func saveLineage(dir string, fullchain, privateKey []byte) error {
	parent := filepath.Dir(dir)
	if err := os.MkdirAll(parent, 0o700); err != nil {
		return err
	}
	// What an interrupted save left beside the lineage is never read.
	if leftovers, err := filepath.Glob(filepath.Join(parent, "."+filepath.Base(dir)+".*")); err == nil {
		for _, leftover := range leftovers {
			_ = os.RemoveAll(leftover)
		}
	}
	staged, err := os.MkdirTemp(parent, "."+filepath.Base(dir)+".")
	if err != nil {
		return err
	}
	defer os.RemoveAll(staged)
	for _, file := range []struct {
		name string
		data []byte
		mode os.FileMode
	}{{"fullchain.pem", fullchain, 0o644}, {"privkey.pem", privateKey, 0o600}} {
		if err := writeNew(filepath.Join(staged, file.name), file.data, file.mode); err != nil {
			return err
		}
	}
	if err := syncDir(staged); err != nil {
		return err
	}
	if err := replaceDir(staged, dir); err != nil {
		return err
	}
	return syncDir(parent)
}

// replaceDir puts staged at dir. An existing dir is exchanged with staged in
// one step and left at staged's name for removal; a filesystem that cannot
// exchange gets two renames, between which a reader finds no lineage and the
// next renewal issues again.
func replaceDir(staged, dir string) error {
	if _, err := os.Lstat(dir); errors.Is(err, os.ErrNotExist) {
		return os.Rename(staged, dir)
	}
	unsupported, err := exchangeDirs(staged, dir)
	if !unsupported {
		return err
	}
	previous := staged + ".previous"
	if err := os.Rename(dir, previous); err != nil {
		return err
	}
	defer os.RemoveAll(previous)
	return os.Rename(staged, dir)
}

// writeNew writes a file that must not exist yet, readable by mode from its
// first byte, and flushed before it is named in a lineage.
func writeNew(path string, data []byte, mode os.FileMode) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, mode)
	if err != nil {
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
	return file.Close()
}

// issueCertificate orders the certificate over DNS-01 and saves it as the
// spec's lineage. Nothing is saved unless the whole order succeeded and the
// certificate covers the spec's names with the key beside it; the challenge
// records are removed either way, after the save.
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
		return nil, nil, &ProviderError{Message: "ACME state is not wholly the controller's: " + foreign + ". A certificate would be issued that the controller cannot save, so nothing was requested."}
	}
	if _, err := r.propagationDelay(); err != nil {
		return nil, nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, acmeOrderTimeout)
	defer cancel()
	client, err := r.acmeClient(ctx, acme)
	if err != nil {
		return nil, nil, err
	}
	result, err := r.orderCertificate(ctx, client, domains)
	if err == nil {
		err = saveIssued(lineage, result, domains)
	}
	if cleanup := r.removeChallenges(ctx, result.placed); cleanup != nil {
		err = errors.Join(err, cleanup)
	}
	if err != nil {
		return nil, nil, err
	}
	return result.fullchain, result.privateKey, nil
}

// saveIssued checks an issued certificate against the spec, then saves it.
func saveIssued(lineage string, result issued, domains []string) error {
	if _, err := validateCertificate(result.fullchain, result.privateKey, domains); err != nil {
		return err
	}
	if err := saveLineage(lineage, result.fullchain, result.privateKey); err != nil {
		return &ProviderError{Message: "the issued certificate could not be saved", Err: err}
	}
	return nil
}
