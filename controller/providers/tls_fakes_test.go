package providers

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"io"
	"io/fs"
	"math/big"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// fakeDialer serves certificates from phases: the phase in effect is the number
// of deployments recorded so far, capped at the last phase.
type fakeDialer struct {
	http   *fakeHTTP
	phases []map[string]fakeServe
	certs  map[string][]byte // name -> DER
}

type fakeServe struct {
	Cert  string `json:"cert"`
	Error string `json:"error"`
}

func (d *fakeDialer) deployments() int {
	count := 0
	for _, request := range d.http.requests {
		if request.method == "RUN" {
			if payload, ok := request.payload.(Object); ok {
				if argv, ok := payload["argv"].([]string); ok && argv[0] == "ssh" && argv[len(argv)-1] == "deploy" {
					count++
				}
			}
		}
		if request.method == "POST" && strings.HasSuffix(request.path, "/upload") {
			count++
		}
	}
	return count
}

func (d *fakeDialer) Peer(_ context.Context, domain, connectHost string) ([]byte, error) {
	d.http.requests = append(d.http.requests, request{"tls://" + connectHost + ":443", "TLS", Object{"sni": domain}, ""})
	if len(d.phases) == 0 {
		return nil, &tlsReadError{reason: "connection refused"}
	}
	phase := d.phases[min(d.deployments(), len(d.phases)-1)]
	serve, ok := phase[connectHost+"|"+domain]
	if !ok {
		return nil, &tlsReadError{reason: "connection refused"}
	}
	if serve.Error != "" {
		return nil, &tlsReadError{reason: serve.Error}
	}
	return d.certs[serve.Cert], nil
}

// fakeCommander answers commands from fixture outcomes and records each one.
type fakeCommander struct {
	http     *fakeHTTP
	outcomes map[string]fakeOutcome
	lineage  map[string]string // certbot certonly writes these files into the lineage
}

type fakeOutcome struct {
	Stdout    string `json:"stdout"`
	StdoutB64 string `json:"stdout_b64"`
	Fail      string `json:"fail"`
}

var stagedPath = regexp.MustCompile(`/[^\s=]*hq-tls-[^/\s]+`)

// commandKey names a command the way fixtures do.
func commandKey(argv []string) string {
	switch argv[0] {
	case "ssh":
		return "ssh " + argv[len(argv)-1]
	case "op":
		return "op " + argv[1] + " " + argv[2]
	case "certbot":
		return "certbot " + argv[1]
	}
	return argv[0]
}

// decodeInput is how a recorded command input is compared: a tar by its
// entries, JSON as parsed, anything else as text.
func decodeInput(input []byte) any {
	if len(input) == 0 {
		return nil
	}
	reader := tar.NewReader(bytes.NewReader(input))
	entries := []Object{}
	for {
		header, err := reader.Next()
		if err != nil {
			if err == io.EOF && len(entries) > 0 {
				return Object{"tar": entries}
			}
			break
		}
		data, _ := io.ReadAll(reader)
		entries = append(entries, Object{"name": header.Name, "mode": header.Mode, "content": string(data)})
	}
	var parsed any
	decoder := json.NewDecoder(bytes.NewReader(input))
	decoder.UseNumber()
	if decoder.Decode(&parsed) == nil {
		return parsed
	}
	return string(input)
}

// exec is the Commands.Exec the fake installs: it answers like a process, so
// the shared runner turns outcomes into its own messages and step records.
func (c *fakeCommander) exec(_ context.Context, commandArgv []string, input []byte, _ []string) ([]byte, []byte, int, error) {
	argv := make([]string, len(commandArgv))
	files := Object{}
	for i, arg := range commandArgv {
		if strings.Contains(arg, "hq-tls-") {
			if label, path, ok := strings.Cut(arg, "[file]="); ok {
				data, _ := os.ReadFile(path)
				files[label] = string(data)
			}
		}
		argv[i] = stagedPath.ReplaceAllString(arg, "<staged>")
	}
	record := Object{"argv": argv, "input": decodeInput(input)}
	if len(files) > 0 {
		record["files"] = files
	}
	key := commandKey(commandArgv)
	if key == "certbot certonly" {
		for i, arg := range commandArgv {
			if arg == "--dns-cloudflare-credentials" {
				data, _ := os.ReadFile(commandArgv[i+1])
				record["credentials"] = string(data)
			}
		}
	}
	c.http.requests = append(c.http.requests, request{commandArgv[0], "RUN", record, ""})
	outcome, ok := c.outcomes[key]
	if !ok && key != "certbot --version" {
		return nil, nil, 1, nil
	}
	switch outcome.Fail {
	case "exit":
		return nil, nil, 1, nil
	case "missing":
		return nil, nil, 0, fs.ErrNotExist
	}
	if key == "certbot certonly" {
		for i, arg := range commandArgv {
			if arg == "--config-dir" {
				name := ""
				for j, other := range commandArgv {
					if other == "--cert-name" {
						name = commandArgv[j+1]
					}
				}
				dir := filepath.Join(commandArgv[i+1], "live", name)
				_ = os.MkdirAll(dir, 0o700)
				for file, content := range c.lineage {
					_ = os.WriteFile(filepath.Join(dir, file), []byte(content), 0o600)
				}
			}
		}
	}
	if outcome.StdoutB64 != "" {
		data, err := base64.StdEncoding.DecodeString(outcome.StdoutB64)
		return data, nil, 0, err
	}
	return []byte(outcome.Stdout), nil, 0, nil
}

// testPKI issues throwaway certificates for one test.
type testPKI struct {
	t      *testing.T
	caKey  *ecdsa.PrivateKey
	caCert *x509.Certificate
}

func newTestPKI(t *testing.T) *testPKI {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Test Root", Organization: []string{"Example CA"}}, NotBefore: time.Unix(0, 0), NotAfter: time.Date(2040, 1, 1, 0, 0, 0, 0, time.UTC), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return &testPKI{t: t, caKey: key, caCert: cert}
}

// leaf returns PEM fullchain (leaf then root) and a PKCS #8 key PEM.
func (p *testPKI) leaf(serial int64, notAfter time.Time, names ...string) ([]byte, []byte, []byte) {
	p.t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	template := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: names[0]}, DNSNames: names, NotBefore: time.Unix(0, 0), NotAfter: notAfter}
	der, err := x509.CreateCertificate(rand.Reader, template, p.caCert, &key.PublicKey, p.caKey)
	if err != nil {
		p.t.Fatal(err)
	}
	keyDER, _ := x509.MarshalPKCS8PrivateKey(key)
	chain := append(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: p.caCert.Raw})...)
	return chain, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}), der
}
