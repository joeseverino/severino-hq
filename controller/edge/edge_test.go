package edge

import (
	"archive/tar"
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	"net"
	"os"
	"os/user"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"
)

var now = time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC)

// fakeDocker stands in for the docker CLI: it records each call, answers
// adapt with $ADAPTED, cat with the host file the path names, and fails the
// verbs its environment says to.
const fakeDocker = `#!/bin/sh
printf '%s\n' "$*" >>"$LOG"
case "$1" in
cp) cat >"$CANDIDATE"; exit "${CP_EXIT:-0}" ;;
inspect) exit "${INSPECT_EXIT:-0}" ;;
esac
if [ "$3" = cat ]; then exec cat "$4"; fi
case "$4" in
adapt) cat "$ADAPTED" ;;
validate) exit "${VALIDATE_EXIT:-0}" ;;
reload) exit "${RELOAD_EXIT:-0}" ;;
esac
`

const fakeSystemctl = `#!/bin/sh
printf '%s\n' "$*" >>"$LOG"
printf "%s\n" "${UNIT_STATE-active}"
exit "${UNIT_EXIT:-0}"
`

// edge is a receiver whose paths and programs all live in one directory.
type edge struct {
	Receiver
	dir string
}

func newEdge(t *testing.T, env ...string) *edge {
	t.Helper()
	dir := t.TempDir()
	for _, sub := range []string{"certs", "config", "bin"} {
		if err := os.Mkdir(filepath.Join(dir, sub), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for name, body := range map[string]string{"docker": fakeDocker, "systemctl": fakeSystemctl} {
		if err := os.WriteFile(filepath.Join(dir, "bin", name), []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	account, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	return &edge{dir: dir,
		CertDir:    filepath.Join(dir, "certs"),
		RoutesFile: filepath.Join(dir, "config", "hq-routes.caddy"),
		Owner:      account.Uid,
		Group:      account.Gid,
		Docker:     filepath.Join(dir, "bin", "docker"),
		Systemctl:  filepath.Join(dir, "bin", "systemctl"),
		Env: append([]string{
			"PATH=/usr/bin:/bin",
			"LOG=" + filepath.Join(dir, "log"),
			"CANDIDATE=" + filepath.Join(dir, "candidate.tar"),
			"ADAPTED=" + filepath.Join(dir, "adapted.json"),
		}, env...),
		Now:       func() time.Time { return now },
		Addresses: func() ([]net.Addr, error) { return nil, nil }}
}

// run is one invocation's exit status and output.
func (e *edge) run(t *testing.T, stdin []byte, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := e.Run(t.Context(), args, bytes.NewReader(stdin), &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

// calls is every program call so far, one line each.
func (e *edge) calls() []string {
	data, _ := os.ReadFile(filepath.Join(e.dir, "log"))
	if len(data) == 0 {
		return nil
	}
	return strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
}

func (e *edge) reloads() int {
	count := 0
	for _, call := range e.calls() {
		if strings.Contains(call, "reload") {
			count++
		}
	}
	return count
}

func (e *edge) read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// certificatePair is a self-signed leaf for name and its key, as PEM, with
// the leaf's SHA-256 fingerprint.
type certificatePair struct {
	chain, key, fingerprint string
}

func issue(t *testing.T, name string, notAfter time.Time) certificatePair {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: name}, DNSNames: []string{name},
		NotBefore: notAfter.Add(-48 * time.Hour), NotAfter: notAfter,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(der)
	return certificatePair{
		chain:       string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
		key:         string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: private})),
		fingerprint: string(sum[:]),
	}
}

// fingerprints is the SHA-256 of every certificate in a PEM stream, and
// whether any other block was in it.
func fingerprints(t *testing.T, data string) ([]string, bool) {
	t.Helper()
	found, other := []string{}, false
	rest := []byte(data)
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			return found, other
		}
		if block.Type != "CERTIFICATE" {
			other = true
			continue
		}
		sum := sha256.Sum256(block.Bytes)
		found = append(found, string(sum[:]))
	}
}

func (e *edge) installPair(t *testing.T, pair certificatePair) {
	t.Helper()
	for name, data := range map[string]string{Fullchain: pair.chain, PrivateKey: pair.key} {
		if err := os.WriteFile(filepath.Join(e.CertDir, name), []byte(data), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func (e *edge) installedFingerprint(t *testing.T) string {
	t.Helper()
	found, _ := fingerprints(t, e.read(t, filepath.Join(e.CertDir, Fullchain)))
	if len(found) != 1 {
		t.Fatalf("installed chain holds %d certificates", len(found))
	}
	return found[0]
}

func TestVersionIsTheSharedConstant(t *testing.T) {
	code, out, _ := newEdge(t).run(t, nil, OperationVersion)
	if code != 0 || out != Version+"\n" {
		t.Fatalf("exit %d, printed %q", code, out)
	}
}

func TestAnOperationNotNamedIsRefusedBeforeAnythingRuns(t *testing.T) {
	for _, args := range [][]string{{"rm-rf"}, {}, {""}, {"routes", "extra"}, {"routes "}, {"ROUTES"}, {"deploy;id"}} {
		t.Run(strings.Join(args, "|"), func(t *testing.T) {
			e := newEdge(t)
			code, out, said := e.run(t, []byte("body"), args...)
			if code != refused || out != "" {
				t.Fatalf("exit %d, printed %q", code, out)
			}
			if !strings.Contains(said, "v"+Version) {
				t.Fatalf("the refusal does not name the version: %q", said)
			}
			if calls := e.calls(); len(calls) != 0 {
				t.Fatalf("started %v", calls)
			}
		})
	}
}

func TestDeployInstallsAMatchedPairAndReloads(t *testing.T) {
	e := newEdge(t)
	e.installPair(t, issue(t, "old.example.test", now.Add(24*time.Hour)))
	fresh := issue(t, "new.example.test", now.Add(24*time.Hour))

	code, _, said := e.run(t, Bundle([]byte(fresh.chain), []byte(fresh.key)), OperationDeploy)

	if code != 0 {
		t.Fatalf("exit %d: %s", code, said)
	}
	if e.installedFingerprint(t) != fresh.fingerprint || e.read(t, filepath.Join(e.CertDir, PrivateKey)) != fresh.key {
		t.Fatal("the new pair was not installed")
	}
	for name, mode := range map[string]os.FileMode{Fullchain: 0o644, PrivateKey: 0o600} {
		info, err := os.Stat(filepath.Join(e.CertDir, name))
		if err != nil || info.Mode().Perm() != mode {
			t.Fatalf("%s mode %v (%v), want %v", name, info.Mode(), err, mode)
		}
	}
	if e.reloads() != 1 {
		t.Fatalf("calls %v, want one reload", e.calls())
	}
}

// A deploy that should be refused leaves the installed pair alone and never
// reloads Caddy.
func TestDeployRefusesWhatCaddyWouldServeWrongly(t *testing.T) {
	good := issue(t, "new.example.test", now.Add(24*time.Hour))
	other := issue(t, "other.example.test", now.Add(24*time.Hour))
	expired := issue(t, "expired.example.test", now.Add(-time.Hour))
	tarOf := func(headers ...tar.Header) []byte {
		var buffer bytes.Buffer
		archive := tar.NewWriter(&buffer)
		for _, header := range headers {
			header.Mode, header.Format = 0o600, tar.FormatUSTAR
			if header.Typeflag == tar.TypeReg {
				header.Size = 4
			}
			if err := archive.WriteHeader(&header); err != nil {
				t.Fatal(err)
			}
			if header.Typeflag == tar.TypeReg {
				_, _ = archive.Write([]byte("data"))
			}
		}
		_ = archive.Close()
		return buffer.Bytes()
	}
	for name, body := range map[string][]byte{
		"a key that does not match the certificate": Bundle([]byte(good.chain), []byte(other.key)),
		"unparseable input":                         Bundle([]byte("not a certificate\n"), []byte("not a key\n")),
		"empty files":                               Bundle(nil, nil),
		"an expired certificate":                    Bundle([]byte(expired.chain), []byte(expired.key)),
		"no body":                                   nil,
		"a body that is not a tar":                  []byte("fullchain.pem privkey.pem"),
		"a third file": tarOf(tar.Header{Name: Fullchain, Typeflag: tar.TypeReg},
			tar.Header{Name: PrivateKey, Typeflag: tar.TypeReg}, tar.Header{Name: "extra.pem", Typeflag: tar.TypeReg}),
		"a file named twice": tarOf(tar.Header{Name: Fullchain, Typeflag: tar.TypeReg},
			tar.Header{Name: PrivateKey, Typeflag: tar.TypeReg}, tar.Header{Name: PrivateKey, Typeflag: tar.TypeReg}),
		"a link in place of the key": tarOf(tar.Header{Name: Fullchain, Typeflag: tar.TypeReg},
			tar.Header{Name: PrivateKey, Typeflag: tar.TypeSymlink, Linkname: "/etc/shadow"}),
		"a path beside the name": tarOf(tar.Header{Name: "./" + Fullchain, Typeflag: tar.TypeReg},
			tar.Header{Name: PrivateKey, Typeflag: tar.TypeReg}),
		"a body past the limit": append(Bundle([]byte(good.chain), []byte(good.key)), make([]byte, MaxBody)...),
	} {
		t.Run(name, func(t *testing.T) {
			e := newEdge(t)
			installed := issue(t, "old.example.test", now.Add(24*time.Hour))
			e.installPair(t, installed)

			code, _, said := e.run(t, body, OperationDeploy)

			if code == 0 {
				t.Fatal("the deploy was accepted")
			}
			if said == "" {
				t.Fatal("the refusal says nothing")
			}
			if e.installedFingerprint(t) != installed.fingerprint || e.read(t, filepath.Join(e.CertDir, PrivateKey)) != installed.key {
				t.Fatal("the installed pair was replaced")
			}
			if calls := e.calls(); len(calls) != 0 {
				t.Fatalf("started %v", calls)
			}
		})
	}
}

func TestDeployRestoresThePreviousPairWhenTheReloadFails(t *testing.T) {
	e := newEdge(t, "RELOAD_EXIT=1")
	installed := issue(t, "old.example.test", now.Add(24*time.Hour))
	e.installPair(t, installed)
	fresh := issue(t, "new.example.test", now.Add(24*time.Hour))

	code, _, said := e.run(t, Bundle([]byte(fresh.chain), []byte(fresh.key)), OperationDeploy)

	if code == 0 || !strings.Contains(said, "restored") {
		t.Fatalf("exit %d: %s", code, said)
	}
	if e.installedFingerprint(t) != installed.fingerprint || e.read(t, filepath.Join(e.CertDir, PrivateKey)) != installed.key {
		t.Fatal("the previous pair was not restored")
	}
	if e.reloads() != 2 {
		t.Fatalf("calls %v, want the restored pair reloaded", e.calls())
	}
}

func TestDeployNeedsAnInstalledPairToFallBackTo(t *testing.T) {
	e := newEdge(t)
	fresh := issue(t, "new.example.test", now.Add(24*time.Hour))
	if code, _, _ := e.run(t, Bundle([]byte(fresh.chain), []byte(fresh.key)), OperationDeploy); code == 0 {
		t.Fatal("a deploy with nothing to roll back to was accepted")
	}
	if _, err := os.Stat(filepath.Join(e.CertDir, Fullchain)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("installed anyway: %v", err)
	}
}

func TestRoutesPassesTheAdaptedConfigThroughUntouched(t *testing.T) {
	e := newEdge(t)
	adapted := `{"apps":{"http":{"servers":{"srv0":{"routes":[]}}}}}`
	if err := os.WriteFile(filepath.Join(e.dir, "adapted.json"), []byte(adapted), 0o600); err != nil {
		t.Fatal(err)
	}
	code, out, said := e.run(t, nil, OperationRoutes)
	if code != 0 || out != adapted {
		t.Fatalf("exit %d, printed %q: %s", code, out, said)
	}
}

func TestCertificateIsTheServedLeafAlone(t *testing.T) {
	e := newEdge(t)
	leaf := issue(t, "new.example.test", now.Add(24*time.Hour))
	issuer := issue(t, "issuer.example.test", now.Add(48*time.Hour))
	e.installPair(t, certificatePair{chain: leaf.chain + issuer.chain, key: leaf.key})

	code, out, said := e.run(t, nil, OperationCertificate)

	found, other := fingerprints(t, out)
	if code != 0 || !slices.Equal(found, []string{leaf.fingerprint}) || other {
		t.Fatalf("exit %d, %d certificates, other blocks %v: %s", code, len(found), other, said)
	}
}

func TestCertificatesIsTheLeafOfEveryFileCaddyLoads(t *testing.T) {
	e := newEdge(t)
	deployed := issue(t, "new.example.test", now.Add(24*time.Hour))
	e.installPair(t, deployed)
	second := issue(t, "preview.example.test", now.Add(24*time.Hour))
	beside := filepath.Join(e.dir, "second.pem")
	if err := os.WriteFile(beside, []byte(second.chain+second.key), 0o600); err != nil {
		t.Fatal(err)
	}
	config, err := json.Marshal(map[string]any{"apps": map[string]any{"tls": map[string]any{"certificates": map[string]any{"load_files": []any{
		map[string]any{"certificate": filepath.Join(e.CertDir, Fullchain), "key": filepath.Join(e.CertDir, PrivateKey), "tags": []string{"cert0"}},
		map[string]any{"certificate": beside, "key": beside, "tags": []string{"cert1"}},
		map[string]any{"certificate": beside, "key": beside, "tags": []string{"cert2"}},
	}}}}})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(e.dir, "adapted.json"), config, 0o600); err != nil {
		t.Fatal(err)
	}

	code, out, said := e.run(t, nil, OperationCertificates)

	found, other := fingerprints(t, out)
	slices.Sort(found)
	want := []string{deployed.fingerprint, second.fingerprint}
	slices.Sort(want)
	if code != 0 || !slices.Equal(found, want) || other {
		t.Fatalf("exit %d, %d certificates, other blocks %v: %s", code, len(found), other, said)
	}
}

func TestCertificatesFailsWhenCaddyLoadsNoFile(t *testing.T) {
	e := newEdge(t)
	if err := os.WriteFile(filepath.Join(e.dir, "adapted.json"), []byte(`{"apps":{}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if code, out, _ := e.run(t, nil, OperationCertificates); code == 0 || out != "" {
		t.Fatalf("exit %d, printed %q", code, out)
	}
}

// candidate is the file docker cp was handed to validate.
func (e *edge) candidate(t *testing.T) string {
	t.Helper()
	archive := tar.NewReader(bytes.NewReader([]byte(e.read(t, filepath.Join(e.dir, "candidate.tar")))))
	header, err := archive.Next()
	if err != nil || header.Name != candidateName {
		t.Fatalf("candidate %v (%v)", header, err)
	}
	data, _ := io.ReadAll(archive)
	if _, err := archive.Next(); err != io.EOF {
		t.Fatalf("the candidate holds more than one file: %v", err)
	}
	return string(data)
}

func TestRoutesWriteValidatesInstallsAndReloads(t *testing.T) {
	e := newEdge(t)
	if err := os.WriteFile(e.RoutesFile, []byte("old.example.test {\n\trespond \"old\" 200\n}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	routes := "new.example.test {\n\trespond \"new\" 200\n}\n"

	code, _, said := e.run(t, []byte(routes), OperationRoutesWrite)

	if code != 0 || e.read(t, e.RoutesFile) != routes || e.candidate(t) != routes {
		t.Fatalf("exit %d: %s", code, said)
	}
	calls := strings.Join(e.calls(), " ")
	if !strings.Contains(calls, "validate") || strings.Index(calls, "validate") > strings.Index(calls, "reload") {
		t.Fatalf("calls %v, want validate before reload", e.calls())
	}
}

func TestRoutesWriteRestoresThePreviousFileWhenTheReloadFails(t *testing.T) {
	for name, previous := range map[string]*string{"a file": new("old.example.test {\n\trespond \"old\" 200\n}\n"), "none": nil} {
		t.Run(name, func(t *testing.T) {
			e := newEdge(t, "RELOAD_EXIT=1")
			if previous != nil {
				if err := os.WriteFile(e.RoutesFile, []byte(*previous), 0o644); err != nil {
					t.Fatal(err)
				}
			}

			code, _, said := e.run(t, []byte("broken.example.test {\n}\n"), OperationRoutesWrite)

			if code == 0 || !strings.Contains(said, "previous routes restored") {
				t.Fatalf("exit %d: %s", code, said)
			}
			want := ""
			if previous != nil {
				want = *previous
			}
			if got := e.read(t, e.RoutesFile); got != want {
				t.Fatalf("holds %q, want %q", got, want)
			}
			if e.reloads() != 2 {
				t.Fatalf("calls %v, want the restored file reloaded", e.calls())
			}
		})
	}
}

func TestRoutesWriteInstallsNothingCaddyRefuses(t *testing.T) {
	for name, test := range map[string]struct {
		env  []string
		body string
	}{
		"an empty file":           {nil, ""},
		"a file validate refuses": {[]string{"VALIDATE_EXIT=1"}, "bad {\n"},
		"a body past the limit":   {nil, strings.Repeat("a", MaxBody+1)},
	} {
		t.Run(name, func(t *testing.T) {
			e := newEdge(t, test.env...)
			kept := "kept.example.test {\n}\n"
			if err := os.WriteFile(e.RoutesFile, []byte(kept), 0o644); err != nil {
				t.Fatal(err)
			}
			if code, _, _ := e.run(t, []byte(test.body), OperationRoutesWrite); code == 0 {
				t.Fatal("accepted")
			}
			if e.read(t, e.RoutesFile) != kept || e.reloads() != 0 {
				t.Fatalf("the routes changed or Caddy reloaded: %v", e.calls())
			}
		})
	}
}

func TestPreflightNeedsThePairAndTheContainer(t *testing.T) {
	pair := issue(t, "old.example.test", now.Add(24*time.Hour))
	ready := newEdge(t)
	ready.installPair(t, pair)
	if code, out, said := ready.run(t, nil, OperationPreflight); code != 0 || out != "" {
		t.Fatalf("exit %d, printed %q: %s", code, out, said)
	}

	empty := newEdge(t)
	empty.installPair(t, certificatePair{chain: pair.chain})
	if code, _, _ := empty.run(t, nil, OperationPreflight); code == 0 {
		t.Fatal("an empty key passed")
	}

	stopped := newEdge(t, "INSPECT_EXIT=3")
	stopped.installPair(t, pair)
	if code, _, _ := stopped.run(t, nil, OperationPreflight); code != 3 {
		t.Fatalf("exit %d, want docker inspect's 3", code)
	}
}

func TestSnapshotIsTheInstalledPairAsABundle(t *testing.T) {
	e := newEdge(t)
	pair := issue(t, "old.example.test", now.Add(24*time.Hour))
	e.installPair(t, pair)
	code, out, said := e.run(t, nil, OperationSnapshot)
	chain, key, err := ReadBundle([]byte(out))
	if code != 0 || err != nil || string(chain) != pair.chain || string(key) != pair.key {
		t.Fatalf("exit %d (%v): %s", code, err, said)
	}
}

func TestPerimeterReportsTheFirewallUnitAndPublicAddresses(t *testing.T) {
	addresses := func(cidrs ...string) func() ([]net.Addr, error) {
		return func() ([]net.Addr, error) {
			found := []net.Addr{}
			for _, cidr := range cidrs {
				ip, network, err := net.ParseCIDR(cidr)
				if err != nil {
					t.Fatal(err)
				}
				network.IP = ip
				found = append(found, network)
			}
			return found, nil
		}
	}
	for name, test := range map[string]struct {
		env       []string
		addresses func() ([]net.Addr, error)
		unit      string
		public    string
	}{
		"active, two public addresses among private ones": {nil, addresses("127.0.0.1/8", "10.0.0.5/8", "172.16.0.1/12", "172.31.9.9/16",
			"192.168.0.0/16", "169.254.1.1/16", "100.64.0.1/10", "100.127.255.1/10", "198.51.100.20/24", "::1/128", "2001:db8::1/64",
			"203.0.113.7/24"), "active", "198.51.100.20,203.0.113.7"},
		"inactive, which still says so": {[]string{"UNIT_STATE=inactive", "UNIT_EXIT=3"}, addresses(), "inactive", ""},
		"a unit that cannot be read":    {[]string{"UNIT_STATE=", "UNIT_EXIT=1"}, addresses(), "unknown", ""},
		"addresses that cannot be read": {nil, func() ([]net.Addr, error) { return nil, errors.New("no netlink") }, "active", ""},
	} {
		t.Run(name, func(t *testing.T) {
			e := newEdge(t, test.env...)
			e.Addresses = test.addresses
			code, out, said := e.run(t, nil, OperationPerimeter)
			if code != 0 {
				t.Fatalf("exit %d: %s", code, said)
			}
			var reading map[string]string
			if err := json.Unmarshal([]byte(out), &reading); err != nil {
				t.Fatal(err)
			}
			want := map[string]string{"record": "perimeter", "firewall_unit": test.unit, "public_addresses": test.public, "read_at": "2026-06-01T12:00:00Z"}
			if len(reading) != len(want) {
				t.Fatalf("reading %v", reading)
			}
			for key, value := range want {
				if reading[key] != value {
					t.Fatalf("%s = %q, want %q", key, reading[key], value)
				}
			}
		})
	}
}

func TestOwnerIsANameOrANumber(t *testing.T) {
	account, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	uid, _ := strconv.Atoi(account.Uid)
	lookup := func(string) (string, error) { return account.Uid, nil }
	if got, err := id(account.Username, lookup); err != nil || got != uid {
		t.Fatalf("by name: %d %v", got, err)
	}
	missing := func(string) (string, error) { return "", errors.New("unknown") }
	if got, err := id("4242", missing); err != nil || got != 4242 {
		t.Fatalf("by number: %d %v", got, err)
	}
	if _, err := id("nobody-here", missing); err == nil {
		t.Fatal("an unknown name resolved")
	}
}
