package providers

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"io"
	"net"
	"os"
	"sort"
	"strconv"
	"syscall"
	"time"
)

// TLSDialer returns the leaf certificate a host serves for one name, verified
// against the system roots and the controller's CA bundle.
type TLSDialer interface {
	Peer(ctx context.Context, domain, connectHost string) ([]byte, error)
}

// NetTLSDialer reads a peer certificate over TCP.
type NetTLSDialer struct {
	CAFile string
}

func (d NetTLSDialer) roots() (*x509.CertPool, error) {
	roots, err := x509.SystemCertPool()
	if err != nil {
		return nil, &ProviderError{Message: "Controller CA bundle could not be loaded."}
	}
	if d.CAFile != "" {
		data, err := os.ReadFile(d.CAFile)
		if err != nil || !roots.AppendCertsFromPEM(data) {
			return nil, &ProviderError{Message: "Controller CA bundle could not be loaded."}
		}
	}
	return roots, nil
}

func (d NetTLSDialer) Peer(ctx context.Context, domain, connectHost string) ([]byte, error) {
	roots, err := d.roots()
	if err != nil {
		return nil, err
	}
	dialer := &net.Dialer{Timeout: tlsDialTimeout}
	ctx, cancel := context.WithTimeout(ctx, tlsDialTimeout)
	defer cancel()
	conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(connectHost, strconv.Itoa(tlsPort)))
	if err != nil {
		return nil, &tlsReadError{kind: socketErrorName(err)}
	}
	defer conn.Close()
	client := tls.Client(conn, &tls.Config{ServerName: domain, RootCAs: roots, MinVersion: tls.VersionTLS12})
	if err := client.HandshakeContext(ctx); err != nil {
		return nil, &tlsReadError{kind: handshakeErrorName(err)}
	}
	peers := client.ConnectionState().PeerCertificates
	if len(peers) == 0 {
		return nil, nil
	}
	return peers[0].Raw, nil
}

// tlsReadError is a failed TLS reading, named by the Python exception class the
// Python controller reports for the same failure.
type tlsReadError struct{ kind string }

func (e *tlsReadError) Error() string { return e.kind }

func socketErrorName(err error) string {
	var dnsErr *net.DNSError
	var netErr net.Error
	switch {
	case errors.As(err, &dnsErr):
		return "gaierror"
	case errors.Is(err, syscall.ECONNREFUSED):
		return "ConnectionRefusedError"
	case errors.Is(err, syscall.ECONNRESET):
		return "ConnectionResetError"
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &netErr) && netErr.Timeout():
		return "TimeoutError"
	}
	return "OSError"
}

func handshakeErrorName(err error) string {
	var verify *tls.CertificateVerificationError
	var hostname x509.HostnameError
	var authority x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	switch {
	case errors.As(err, &verify), errors.As(err, &hostname), errors.As(err, &authority), errors.As(err, &invalid):
		return "SSLCertVerificationError"
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return "SSLEOFError"
	case errors.Is(err, syscall.ECONNRESET):
		return "ConnectionResetError"
	case errors.Is(err, context.DeadlineExceeded):
		return "TimeoutError"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "TimeoutError"
	}
	return "SSLError"
}

// observeTLS reads what connectHost serves for domain.
func (r *Registry) observeTLS(ctx context.Context, domain, connectHost string) (TLSObservation, error) {
	if connectHost == "" {
		connectHost = domain
	}
	der, err := r.TLS.Peer(ctx, domain, connectHost)
	if err != nil {
		var read *tlsReadError
		if errors.As(err, &read) {
			return TLSObservation{}, &ProviderError{Message: "TLS observation failed for " + domain + ": " + read.kind + "."}
		}
		var provider *ProviderError
		if errors.As(err, &provider) {
			return TLSObservation{}, provider
		}
		return TLSObservation{}, &ProviderError{Message: "TLS observation failed for " + domain + ": OSError."}
	}
	if len(der) == 0 {
		return TLSObservation{}, &ProviderError{Message: "TLS observation returned no certificate for " + domain + "."}
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		return TLSObservation{}, &ProviderError{Message: "TLS expiry was invalid for " + domain + "."}
	}
	digest := sha256.Sum256(der)
	sans := append([]string{}, certificate.DNSNames...)
	sort.Strings(sans)
	return TLSObservation{
		Domain:            domain,
		NotAfter:          isoUTC(certificate.NotAfter),
		FingerprintSHA256: hex.EncodeToString(digest[:]),
		Issuer:            issuerName(certificate),
		SANs:              sans,
		certificatePEM:    string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
	}, nil
}

// issuerName is the issuer's organization, else its common name, as the last
// value of each the certificate carries.
func issuerName(certificate *x509.Certificate) string {
	organizations := certificate.Issuer.Organization
	if len(organizations) > 0 && organizations[len(organizations)-1] != "" {
		return organizations[len(organizations)-1]
	}
	if certificate.Issuer.CommonName != "" {
		return certificate.Issuer.CommonName
	}
	return "Unknown"
}

// isoUTC is a moment as Python's datetime.isoformat() writes a whole-second UTC time.
func isoUTC(moment time.Time) string {
	return moment.UTC().Truncate(time.Second).Format("2006-01-02T15:04:05+00:00")
}
