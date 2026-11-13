// Package edge is the receiver on a Caddy edge that HQ's controller deploys
// certificates and routes to over SSH, and the wire the two share: the
// version, the operation names and the certificate bundle.
//
// The receiver runs as root. The edge key's forced command is
// deploy/targets/severino-hq-edge-dispatch, which hands the client's command
// to it through sudo as one argument, so what it trusts is fixed here:
//
//   - It takes exactly one argument, the operation, matched whole against
//     Operations. Anything else is refused with exit 126 before anything is
//     read or started.
//   - It reads standard input only for deploy and routes:write, at most
//     MaxBody bytes. A deploy body is a Bundle of exactly fullchain.pem and
//     privkey.pem, both regular files, whose key is the leaf's and whose leaf
//     has not expired; a routes body is not empty and passes Caddy's own
//     validate before it is installed.
//   - It writes only the two certificate files and the HQ-owned routes file,
//     each put in place whole through atomicfile with its owner and mode set
//     before the rename, and restores the previous contents when Caddy's
//     reload fails.
//   - Besides what it installs it reads only whether the machine waits on a
//     restart: the flag at RebootRequired and its package list, which are
//     world-readable, through hostcore.ReadReboot.
//   - It starts docker, systemctl and busctl only through hostcore.Process,
//     handing in Env as the whole environment, with standard input closed.
package edge

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"io"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
)

// Version is bumped whenever an operation is added. A controller that finds
// an operation refused tells the operator to redeploy the edge at this
// version, so a stale edge reads as one.
const Version = "5"

// The operations an edge answers.
const (
	OperationVersion   = "version"
	OperationPreflight = "preflight"
	OperationSnapshot  = "snapshot"
	OperationPerimeter = "perimeter"
	// OperationReboot is whether the machine waits on a restart, as
	// hostcore.RebootReading; an edge older than version 5 refuses it.
	OperationReboot       = "reboot"
	OperationRoutes       = "routes"
	OperationRoutesWrite  = "routes:write"
	OperationDeploy       = "deploy"
	OperationCertificates = "certificates"
	// OperationCertificate is what an edge older than version 4 answers in
	// place of OperationCertificates: the one leaf in its certificate
	// directory.
	OperationCertificate = "certificate"
)

// MaxBody bounds what deploy and routes:write read: a certificate bundle and a
// routes file are a few kilobytes.
const MaxBody = 1 << 20

// refused is the exit status of an operation the edge does not answer.
const refused = 126

// The container the edge's Caddy runs in, and its config inside it.
const (
	caddyContainer = "caddy"
	caddyfile      = "/etc/caddy/Caddyfile"
	// candidate is where a routes file is validated inside the container.
	candidateDir  = "/tmp"
	candidateName = "hq-routes.candidate"
)

// Receiver is one edge's paths and programs; every one is a field so a test
// can relocate the whole edge.
type Receiver struct {
	// CertDir holds the certificate Caddy serves, fullchain.pem and privkey.pem.
	CertDir string
	// RoutesFile is the HQ-owned file the operator's Caddyfile imports.
	RoutesFile string
	// Owner and Group own what the receiver installs, by name or number.
	Owner, Group string
	// Docker, Systemctl and Busctl are the programs it starts.
	Docker, Systemctl, Busctl string
	// RebootRequired is the flag an update leaves when it waits on a restart
	// (hostcore.RebootRequired), and Born when a file was created
	// (hostcore.Born).
	RebootRequired string
	Born           func(path string) (time.Time, error)
	// Env is every program's whole environment.
	Env []string
	// Now is the clock an expiry and a reading are judged by.
	Now func() time.Time
	// Addresses are this machine's interface addresses.
	Addresses func() ([]net.Addr, error)
}

// Defaults is the receiver an edge runs: Caddy's directories under
// /opt/apps/caddy, root's files, root's environment.
func Defaults(getenv func(string) string) Receiver {
	value := func(name, fallback string) string {
		if found := getenv(name); found != "" {
			return found
		}
		return fallback
	}
	return Receiver{
		CertDir:        value("SEVERINO_HQ_CADDY_CERT_DIR", "/opt/apps/caddy/certs"),
		RoutesFile:     value("SEVERINO_HQ_CADDY_ROUTES", "/opt/apps/caddy/config/hq-routes.caddy"),
		Owner:          value("SEVERINO_HQ_CERT_OWNER", "root"),
		Group:          value("SEVERINO_HQ_CERT_GROUP", "root"),
		Docker:         "docker",
		Systemctl:      "systemctl",
		Busctl:         hostcore.ScheduledShutdown[0],
		RebootRequired: hostcore.RebootRequired,
		Born:           hostcore.Born,
		Env:            hostcore.RootEnvironment(),
		Now:            time.Now,
		Addresses:      net.InterfaceAddrs,
	}
}

// failure is a refusal with the exit status it ends the run with.
type failure struct {
	message string
	code    int
}

func (f *failure) Error() string { return f.message }

func refuse(format string, args ...any) error {
	return &failure{message: fmt.Sprintf(format, args...), code: 1}
}

// Run answers one invocation and is its exit status.
func (r Receiver) Run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	err := r.answer(ctx, args, stdin, stdout, stderr)
	var stopped *failure
	var exited *exec.ExitError
	switch {
	case err == nil:
		return 0
	case errors.As(err, &stopped):
		fmt.Fprintln(stderr, err)
		return stopped.code
	case errors.As(err, &exited):
		// The program said why on the stderr it was handed.
		return exited.ExitCode()
	default:
		fmt.Fprintln(stderr, err)
		return 1
	}
}

// Operations is every operation the receiver answers.
func Operations() []string {
	return []string{
		OperationVersion, OperationPreflight, OperationSnapshot, OperationPerimeter, OperationReboot,
		OperationRoutes, OperationRoutesWrite, OperationDeploy, OperationCertificates, OperationCertificate,
	}
}

func (r Receiver) answer(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) error {
	operation := ""
	if len(args) > 0 {
		operation = args[0]
	}
	if len(args) > 1 || !slices.Contains(Operations(), operation) {
		// The operation and the version, so a stale edge reads as one.
		return &failure{code: refused, message: fmt.Sprintf("Severino HQ edge target v%s does not implement %q.\n"+
			"If the controller expects it, this target is older than the controller.", Version, strings.Join(args, " "))}
	}
	switch operation {
	case OperationVersion:
		_, err := fmt.Fprintln(stdout, Version)
		return err
	case OperationPreflight:
		return r.preflight(ctx, stderr)
	case OperationSnapshot:
		return r.snapshot(stdout)
	case OperationPerimeter:
		return r.perimeter(ctx, stdout)
	case OperationReboot:
		return r.reboot(ctx, stdout, stderr)
	case OperationRoutes:
		// Emitted whole and parsed by HQ, so the part likely to change stays
		// in the repository rather than on a host that takes a root install.
		return r.docker(ctx, stdout, stderr, "exec", caddyContainer, "caddy", "adapt", "--config", caddyfile)
	case OperationCertificate:
		return r.certificate(stdout)
	case OperationCertificates:
		return r.certificates(ctx, stdout, stderr)
	case OperationRoutesWrite:
		body, err := readBody(stdin)
		if err != nil {
			return err
		}
		return r.writeRoutes(ctx, body, stdout, stderr)
	default: // OperationDeploy
		body, err := readBody(stdin)
		if err != nil {
			return err
		}
		return r.deploy(ctx, body, stdout, stderr)
	}
}

func readBody(stdin io.Reader) ([]byte, error) {
	body, err := io.ReadAll(io.LimitReader(stdin, MaxBody+1))
	if err != nil {
		return nil, err
	}
	if len(body) > MaxBody {
		return nil, refuse("Refusing: the body is larger than %d bytes.", MaxBody)
	}
	return body, nil
}

// process is a program started with Env as its whole environment and its
// standard input closed.
func (r Receiver) process(ctx context.Context, name string, args ...string) *exec.Cmd {
	return hostcore.Process(ctx, r.Env, name, args...)
}

// docker runs one docker command, its output passed through.
func (r Receiver) docker(ctx context.Context, stdout, stderr io.Writer, args ...string) error {
	command := r.process(ctx, r.Docker, args...)
	command.Stdout, command.Stderr = stdout, stderr
	return command.Run()
}

// dockerOutput is one docker command's standard output.
func (r Receiver) dockerOutput(ctx context.Context, stderr io.Writer, args ...string) ([]byte, error) {
	var out bytes.Buffer
	err := r.docker(ctx, &out, stderr, args...)
	return out.Bytes(), err
}

func (r Receiver) reload(ctx context.Context, stdout, stderr io.Writer) error {
	return r.docker(ctx, stdout, stderr, "exec", caddyContainer, "caddy", "reload", "--config", caddyfile, "--force")
}

func (r Receiver) preflight(ctx context.Context, stderr io.Writer) error {
	for _, name := range []string{Fullchain, PrivateKey} {
		info, err := os.Stat(filepath.Join(r.CertDir, name))
		if err != nil || info.Size() == 0 {
			return refuse("%s is missing or empty.", filepath.Join(r.CertDir, name))
		}
	}
	return r.docker(ctx, io.Discard, stderr, "inspect", caddyContainer)
}

// snapshot is the installed pair as a Bundle, the rollback a deploy keeps.
func (r Receiver) snapshot(stdout io.Writer) error {
	fullchain, privateKey, err := r.installed()
	if err != nil {
		return err
	}
	_, err = stdout.Write(Bundle(fullchain, privateKey))
	return err
}

func (r Receiver) installed() (fullchain, privateKey []byte, err error) {
	if fullchain, err = os.ReadFile(filepath.Join(r.CertDir, Fullchain)); err != nil {
		return nil, nil, err
	}
	if privateKey, err = os.ReadFile(filepath.Join(r.CertDir, PrivateKey)); err != nil {
		return nil, nil, err
	}
	return fullchain, privateKey, nil
}

// certificate is the leaf Caddy serves from the deployed directory: public,
// where snapshot would carry the key.
func (r Receiver) certificate(stdout io.Writer) error {
	data, err := os.ReadFile(filepath.Join(r.CertDir, Fullchain))
	if err != nil {
		return err
	}
	leaf, err := leafPEM(data)
	if err != nil {
		return err
	}
	_, err = stdout.Write(leaf)
	return err
}

// leafPEM is the first certificate in data and nothing else: no chain, no key.
func leafPEM(data []byte) ([]byte, error) {
	leaf, err := FirstCertificate(data)
	if err != nil {
		return nil, refuse("the certificate does not parse: %v", err)
	}
	return pemEncode(leaf.Raw), nil
}

// loadedFile is a certificate path in the config Caddy adapts.
var loadedFile = regexp.MustCompile(`"certificate":"(/[^"]*)"`)

// certificates is the leaf of every certificate file Caddy loads, one after
// another, so one the operator's Caddyfile loads beside the deployed one is
// reported too. Each is read inside the container, where its path means
// something.
func (r Receiver) certificates(ctx context.Context, stdout, stderr io.Writer) error {
	config, err := r.dockerOutput(ctx, stderr, "exec", caddyContainer, "caddy", "adapt", "--config", caddyfile)
	if err != nil {
		return err
	}
	paths := []string{}
	for _, match := range loadedFile.FindAllSubmatch(config, -1) {
		paths = append(paths, string(match[1]))
	}
	slices.Sort(paths)
	paths = slices.Compact(paths)
	if len(paths) == 0 {
		return refuse("Caddy loads no certificate from a file.")
	}
	var leaves bytes.Buffer
	for _, path := range paths {
		data, err := r.dockerOutput(ctx, stderr, "exec", caddyContainer, "cat", path)
		if err != nil {
			return err
		}
		leaf, err := leafPEM(data)
		if err != nil {
			return err
		}
		leaves.Write(leaf)
	}
	_, err = stdout.Write(leaves.Bytes())
	return err
}

// writeRoutes installs the routes HQ declares, and only those, in the file of
// their own the operator's Caddyfile imports. The file is validated alone
// first, while the running config is still the old one; a conflict with the
// operator's file only shows once the two are one config, so a reload that
// fails puts the previous file back.
func (r Receiver) writeRoutes(ctx context.Context, body []byte, stdout, stderr io.Writer) error {
	if len(body) == 0 {
		return refuse("Refusing: the routes file is empty.")
	}
	copyIn := r.process(ctx, r.Docker, "cp", "-", caddyContainer+":"+candidateDir)
	copyIn.Stdin = bytes.NewReader(candidateArchive(body))
	copyIn.Stdout, copyIn.Stderr = stdout, stderr
	if err := copyIn.Run(); err != nil {
		return err
	}
	if err := r.docker(ctx, stdout, stderr, "exec", caddyContainer, "caddy", "validate", "--adapter", "caddyfile",
		"--config", candidateDir+"/"+candidateName); err != nil {
		return err
	}
	previous, err := os.ReadFile(r.RoutesFile)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := r.install(r.RoutesFile, body, 0o644); err != nil {
		return err
	}
	if err := r.reload(ctx, stdout, stderr); err != nil {
		return r.restore(ctx, stdout, stderr, "Severino HQ route reload failed; previous routes restored.",
			installation{r.RoutesFile, previous, 0o644})
	}
	return nil
}

// candidateArchive is the tar docker cp extracts into the container: the
// candidate file alone.
func candidateArchive(body []byte) []byte {
	var buffer bytes.Buffer
	writeTar(&buffer, tarFile{candidateName, body})
	return buffer.Bytes()
}

// deploy installs a certificate and its key and reloads Caddy, putting the
// previous pair back when the reload fails. A key that is not the leaf's, or
// a leaf already expired, is refused before anything is written: either would
// reload without complaint and be served until a browser objected.
func (r Receiver) deploy(ctx context.Context, body []byte, stdout, stderr io.Writer) error {
	fullchain, privateKey, err := ReadBundle(body)
	if err != nil {
		return refuse("Refusing: %v.", err)
	}
	leaf, err := Pair(fullchain, privateKey)
	switch {
	case errors.Is(err, ErrKeyMismatch):
		return refuse("Refusing: the private key does not match the certificate.")
	case err != nil:
		return refuse("Refusing: %v.", strings.ReplaceAll(err.Error(), "\n", ": "))
	case !r.Now().Before(leaf.NotAfter):
		return refuse("Refusing: the certificate has already expired.")
	}
	previousChain, previousKey, err := r.installed()
	if err != nil {
		return err
	}
	if err := r.install(filepath.Join(r.CertDir, Fullchain), fullchain, 0o644); err != nil {
		return err
	}
	if err := r.install(filepath.Join(r.CertDir, PrivateKey), privateKey, 0o600); err != nil {
		return errors.Join(err, r.put(installation{filepath.Join(r.CertDir, Fullchain), previousChain, 0o644}))
	}
	if err := r.reload(ctx, stdout, stderr); err != nil {
		return r.restore(ctx, stdout, stderr, "Severino HQ certificate reload failed; previous certificate restored.",
			installation{filepath.Join(r.CertDir, Fullchain), previousChain, 0o644},
			installation{filepath.Join(r.CertDir, PrivateKey), previousKey, 0o600})
	}
	return nil
}

// installation is one file's contents and mode.
type installation struct {
	path string
	data []byte
	mode fs.FileMode
}

func (r Receiver) put(files ...installation) error {
	for _, file := range files {
		if err := r.install(file.path, file.data, file.mode); err != nil {
			return err
		}
	}
	return nil
}

// restore puts files back after a failed reload and reloads them, failing
// with message either way: the operation did not take.
func (r Receiver) restore(ctx context.Context, stdout, stderr io.Writer, message string, files ...installation) error {
	if err := r.put(files...); err != nil {
		return errors.Join(refuse("%s", message), err)
	}
	if err := r.reload(ctx, stdout, stderr); err != nil {
		return errors.Join(refuse("%s", message), err)
	}
	return refuse("%s", message)
}

// install puts data at path whole, owned by Owner and Group with mode, both
// set before the rename so the file is never readable wider than its mode.
func (r Receiver) install(path string, data []byte, mode fs.FileMode) error {
	uid, err := id(r.Owner, func(name string) (string, error) {
		found, err := user.Lookup(name)
		if err != nil {
			return "", err
		}
		return found.Uid, nil
	})
	if err != nil {
		return err
	}
	gid, err := id(r.Group, func(name string) (string, error) {
		found, err := user.LookupGroup(name)
		if err != nil {
			return "", err
		}
		return found.Gid, nil
	})
	if err != nil {
		return err
	}
	file, err := atomicfile.Create(path, mode)
	if err != nil {
		return err
	}
	defer file.Discard()
	if err := file.Chown(uid, gid); err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		return err
	}
	return file.Replace()
}

// id is a user or group by name, or by number as install(1) takes one.
func id(name string, lookup func(string) (string, error)) (int, error) {
	if found, err := lookup(name); err == nil {
		return strconv.Atoi(found)
	}
	number, err := strconv.Atoi(name)
	if err != nil || number < 0 {
		return 0, fmt.Errorf("no user or group %q", name)
	}
	return number, nil
}
