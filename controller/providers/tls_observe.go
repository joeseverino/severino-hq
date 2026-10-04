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
		return nil, &ProviderError{Message: "controller CA bundle could not be loaded"}
	}
	if d.CAFile != "" {
		data, err := os.ReadFile(d.CAFile)
		if err != nil || !roots.AppendCertsFromPEM(data) {
			return nil, &ProviderError{Message: "controller CA bundle could not be loaded"}
		}
	}
	return roots, nil
}

func (d NetTLSDialer) Peer(ctx context.Context, domain, connectHost string) ([]byte, error) {
	return d.peerAt(ctx, domain, net.JoinHostPort(connectHost, strconv.Itoa(tlsPort)))
}

// peerAt reads the leaf address serves for domain, verified chain and name.
func (d NetTLSDialer) peerAt(ctx context.Context, domain, address string) ([]byte, error) {
	roots, err := d.roots()
	if err != nil {
		return nil, err
	}
	dialer := &net.Dialer{Timeout: tlsDialTimeout}
	ctx, cancel := context.WithTimeout(ctx, tlsDialTimeout)
	defer cancel()
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, dialFailure(err)
	}
	defer conn.Close()
	client := tls.Client(conn, &tls.Config{ServerName: domain, RootCAs: roots, MinVersion: tls.VersionTLS12})
	if err := client.HandshakeContext(ctx); err != nil {
		return nil, handshakeFailure(err)
	}
	peers := client.ConnectionState().PeerCertificates
	if len(peers) == 0 {
		return nil, nil
	}
	return peers[0].Raw, nil
}

// tlsReadError is a failed TLS reading: why, in a few words, with the cause.
type tlsReadError struct {
	reason string
	err    error
}

func (e *tlsReadError) Error() string { return e.reason }
func (e *tlsReadError) Unwrap() error { return e.err }

// transportFailureReason is the reset/timeout policy shared by TCP and TLS.
func transportFailureReason(err error, fallback string) string {
	var netErr net.Error
	switch {
	case errors.Is(err, syscall.ECONNRESET):
		return "connection reset"
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &netErr) && netErr.Timeout():
		return "timed out"
	default:
		return fallback
	}
}

// dialFailure names why a TCP connection to a consumer failed.
func dialFailure(err error) *tlsReadError {
	var dnsErr *net.DNSError
	reason := ""
	switch {
	case errors.As(err, &dnsErr):
		reason = "name does not resolve"
	case errors.Is(err, syscall.ECONNREFUSED):
		reason = "connection refused"
	default:
		reason = transportFailureReason(err, "connection failed")
	}
	return &tlsReadError{reason: reason, err: err}
}

// handshakeFailure names why a TLS handshake with a consumer failed.
func handshakeFailure(err error) *tlsReadError {
	var verify *tls.CertificateVerificationError
	var hostname x509.HostnameError
	var authority x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	reason := ""
	switch {
	case errors.As(err, &verify), errors.As(err, &hostname), errors.As(err, &authority), errors.As(err, &invalid):
		reason = "certificate not trusted for this name"
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		reason = "connection closed during handshake"
	default:
		reason = transportFailureReason(err, "handshake failed")
	}
	return &tlsReadError{reason: reason, err: err}
}

// observeTLS reads what connectHost serves for domain.
func (r *Registry) observeTLS(ctx context.Context, domain, connectHost string) (TLSObservation, error) {
	if connectHost == "" {
		connectHost = domain
	}
	der, err := r.TLS.Peer(ctx, domain, connectHost)
	if err != nil {
		if isProviderError(err) {
			return TLSObservation{}, err
		}
		return TLSObservation{}, &ProviderError{Message: "TLS read of " + domain, Err: err}
	}
	if len(der) == 0 {
		return TLSObservation{}, &ProviderError{Message: "TLS read of " + domain + " returned no certificate"}
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		return TLSObservation{}, &ProviderError{Message: "TLS read of " + domain + ": certificate unreadable", Err: err}
	}
	digest := sha256.Sum256(der)
	sans := append([]string{}, certificate.DNSNames...)
	sort.Strings(sans)
	return TLSObservation{
		Domain:            domain,
		NotAfter:          stamp(certificate.NotAfter.Truncate(time.Second)),
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
