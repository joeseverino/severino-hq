package host

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
)

// Where the controller's container finds what it is given: each path is the
// mount's target and the variable that names it, from one declaration.
const (
	controllerConnections = "/run/secrets/controller-connections.json"
	controllerSSH         = "/run/secrets/controller-ssh"
	controllerCA          = "/run/secrets/severino_controller_ca.pem"
	controllerACME        = "/var/lib/severino-hq/acme"
)

// Controller runs one controller pass in a short-lived container made from
// the image HQ is running. The web container never receives a provider
// credential, identity, ACME state or private key; the controller's
// container never receives HQ's database or application environment, and
// reaches HQ only through the bridge socket.
type Controller struct {
	*Machine
	// SecretDir is the renderer's tmpfs (SecretDir on a host).
	SecretDir string
	// ACME is the controller's certificate state (ACMEDir on a host).
	ACME string
	// CertStore holds the roots this host added to its trust store.
	CertStore string
	// Tailscaled is the tailscale daemon's local API socket.
	Tailscaled string
	// UnitSocket is root's socket for a container's unit (UnitSocket on a
	// host); its directory is lent to the pass when it is there.
	UnitSocket string
	// Spool is root's own directory a valid dependency handoff is moved to
	// (RenovateSpool on a host), and DependencyRun the outcome of the last
	// run (DependencyRunFile), given to the next pass.
	Spool, DependencyRun string
	// Connections checks the connections document before it is read.
	Connections func() error
	// Lock holds the renderer's identity lock shared until the returned
	// function is called, so a copy holds one generation.
	Lock func(ctx context.Context) (func(), error)
	// Owner gives name, beneath root, to the controller's account. Every path
	// is resolved beneath root, so a link cannot carry a change outside it.
	Owner func(root *os.Root, name string) error
	// LookPath finds a host program to lend the container.
	LookPath func(name string) (string, error)
	Now      func() time.Time
	// Stdout and Stderr receive the controller's own output.
	Stdout, Stderr io.Writer
}

// Pass is what one launch asks of the controller.
type Pass int

const (
	// Plan looks at what is queued and changes nothing.
	Plan Pass = iota
	// Apply executes queued work and sweeps.
	Apply
	// Glance reads only the dashboard panels HQ is waiting on, beside any
	// other pass. Its container is given less than a controller pass's: the
	// connections the glance reads and their identities, the host's roots and
	// the bridge; no certificate state, unit socket, host reading or way to
	// hand work back to root.
	Glance
)

// PortainerProvider is the provider name of a Portainer connection, as the
// controller's registry declares it (held to it by a test there).
const PortainerProvider = "portainer"

// glanceReads is whether a glance pass is given a connection: what it reads a
// machine through, Portainer or an SSH transport, whatever provider names it.
func glanceReads(connection connections.Connection) bool {
	return connection.Provider == PortainerProvider || connection.SSHTransport != nil
}

// Run launches one pass.
func (c *Controller) Run(ctx context.Context, pass Pass) error {
	if err := c.Connections(); err != nil {
		return err
	}
	if pass != Glance {
		if err := c.prepareACME(); err != nil {
			return err
		}
	}
	c.pruneRuns()
	run, err := os.MkdirTemp(c.SecretDir, "run.")
	if err != nil {
		return err
	}
	defer os.RemoveAll(run)

	if err := c.copySecrets(ctx, run, pass == Glance); err != nil {
		return err
	}
	image, source, err := c.image(ctx)
	if err != nil {
		return err
	}
	nonce := make([]byte, 16)
	if _, err := rand.Read(nonce); err != nil {
		return errors.New("no randomness for the run nonce")
	}
	role := "controller"
	if pass == Glance {
		role = "glance"
	}
	args := []string{
		"run", "--rm", "--network", "host", "--user", fmt.Sprintf("%d:%d", WebUID, WebGID), "--cap-drop", "ALL",
		"--no-healthcheck",
		// A nonce the pass is handed too, so it can tell which container it
		// finds is itself: a fixed label any container could set.
		"--label", "severino-hq.role=" + role,
		"--label", "severino-hq.run=" + hex.EncodeToString(nonce),
		"--env", "HQ_CONTROLLER_RUN=" + hex.EncodeToString(nonce),
		"--security-opt", "no-new-privileges:true",
		"--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev",
		"--entrypoint", "/usr/local/bin/hq-controller",
		"--mount", readOnly(filepath.Join(run, "connections.json"), controllerConnections),
		"--mount", readOnly(filepath.Join(run, "ssh"), controllerSSH),
		"--env", "HQ_CONTROLLER_CONNECTIONS=" + controllerConnections,
		"--env", "HQ_CONTROLLER_SSH_DIR=" + controllerSSH,
	}
	if pass == Glance {
		roots, err := c.roots(run)
		if err != nil {
			return err
		}
		args = append(args, roots...)
	} else {
		if err := c.prepareOut(run); err != nil {
			return err
		}
		args = append(args,
			"--mount", "type=bind,source="+c.ACME+",target="+controllerACME,
			"--mount", "type=bind,source="+filepath.Join(run, "out")+",target="+controllerOut,
			"--env", controllerOutEnv+"="+controllerOut,
			"--env", "HQ_ACME_DIR="+controllerACME,
			"--env", "HQ_CONTROLLER_IMAGE="+image,
			"--env", "SEVERINO_HQ_SOURCE_REPOSITORY="+source,
		)
		readings, err := c.readings(ctx, run)
		if err != nil {
			return err
		}
		args = append(args, readings...)
		args = append(args, c.unitSocket()...)
	}
	// The bridge, from the declaration the web container is given it by.
	args = append(args,
		"--mount", "type=volume,source="+bridgeVolume()+",target="+filepath.Dir(BridgeSocket)+",readonly",
		"--env", "SEVERINO_BRIDGE_SOCKET="+BridgeSocket,
		image)
	switch pass {
	case Apply:
		args = append(args, "--apply")
	case Glance:
		args = append(args, "--glance")
	}
	command := c.Command(ctx, "docker", args...)
	command.Stdout, command.Stderr = c.Stdout, c.Stderr
	err = command.Run()
	if pass != Glance {
		// A pass that failed after minting still leaves tokens worth running.
		c.takeHandoff(run)
	}
	return err
}

// unitSocket lends the pass the unit socket's directory, the only way it
// asks root to act on a container's unit. A bind's source must exist, so a
// host whose socket unit has not been installed yet lends nothing, and the
// controller refuses that work as out of reach.
func (c *Controller) unitSocket() []string {
	dir := filepath.Dir(c.UnitSocket)
	if info, err := os.Lstat(dir); err != nil || !info.IsDir() {
		return nil
	}
	target := filepath.Dir(UnitSocket)
	return []string{
		"--mount", "type=bind,source=" + dir + ",target=" + target,
		"--env", "SEVERINO_UNIT_SOCKET=" + filepath.Join(target, filepath.Base(c.UnitSocket)),
	}
}

func readOnly(source, target string) string {
	return "type=bind,source=" + source + ",target=" + target + ",readonly"
}

func bridgeVolume() string {
	for _, volume := range Volumes {
		if volume.Target == filepath.Dir(BridgeSocket) {
			return volume.Name
		}
	}
	panic("the bridge socket's directory is not a declared volume")
}

// prepareACME makes the certificate state the controller's, every run and
// the whole tree: issuance replaces a lineage directory and the files in it,
// which an unprivileged process cannot do to what another account owns.
func (c *Controller) prepareACME() error {
	if err := os.MkdirAll(c.ACME, 0o700); err != nil {
		return err
	}
	return c.ownTree(c.ACME)
}

// ownTree gives dir and everything beneath it to the controller's account.
func (c *Controller) ownTree(dir string) error {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return err
	}
	defer root.Close()
	return fs.WalkDir(root.FS(), ".", func(name string, _ fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return c.Owner(root, name)
	})
}

// pruneRuns removes what a run killed before its cleanup left behind.
func (c *Controller) pruneRuns() {
	entries, _ := os.ReadDir(c.SecretDir)
	for _, entry := range entries {
		if !entry.IsDir() || !strings.HasPrefix(entry.Name(), "run.") {
			continue
		}
		if info, err := entry.Info(); err == nil && c.Now().Sub(info.ModTime()) > 2*time.Hour {
			os.RemoveAll(filepath.Join(c.SecretDir, entry.Name()))
		}
	}
}

// copySecrets copies the connections document and the identities into the
// run under the renderer's lock, held shared, so the run holds one
// generation if a render replaces them meanwhile. A glance's document holds
// only the connections it reads.
func (c *Controller) copySecrets(ctx context.Context, run string, glance bool) error {
	if err := os.Mkdir(filepath.Join(run, "ssh"), 0o700); err != nil {
		return err
	}
	unlock, err := c.Lock(ctx)
	if err != nil {
		return err
	}
	document := filepath.Join(c.SecretDir, "controller-connections.json")
	err = c.Connections()
	if err == nil && glance {
		err = copyGlanceConnections(document, filepath.Join(run, "connections.json"))
	} else if err == nil {
		err = copyFile(document, filepath.Join(run, "connections.json"), 0o400)
	}
	if err == nil {
		err = copyTree(filepath.Join(c.SecretDir, "ssh"), filepath.Join(run, "ssh"))
	}
	unlock()
	if err != nil {
		return err
	}
	return c.ownTree(run)
}

// copyGlanceConnections writes to the connections in from that a glance reads.
func copyGlanceConnections(from, to string) error {
	data, err := os.ReadFile(from)
	if err != nil {
		return err
	}
	document, err := connections.Decode(data)
	if err != nil {
		return err
	}
	document.Connections = slices.DeleteFunc(document.Connections, func(connection connections.Connection) bool {
		return !glanceReads(connection)
	})
	if data, err = document.Encode(); err != nil {
		return err
	}
	return os.WriteFile(to, data, 0o400)
}

// copyTree copies the regular files and directories under from into to, if
// from exists. A link is not followed.
func copyTree(from, to string) error {
	err := filepath.WalkDir(from, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		target := filepath.Join(to, strings.TrimPrefix(path, from))
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case entry.IsDir():
			return os.MkdirAll(target, info.Mode().Perm())
		case entry.Type().IsRegular():
			return copyFile(path, target, info.Mode().Perm())
		}
		return nil
	})
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

var repository = regexp.MustCompile(`^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$`)

// image is what HQ runs, and the repository its OCI source label names,
// blank when it names none: delivery then has nothing to follow.
func (c *Controller) image(ctx context.Context) (string, string, error) {
	out, err := c.Command(ctx, "docker", "inspect", "--format", "{{.Config.Image}}", Container).Output()
	if err != nil {
		return "", "", errors.New("could not resolve the image HQ runs")
	}
	image, err := Pinned(strings.TrimSpace(string(out)))
	if err != nil {
		return "", "", err
	}
	label, _ := c.Command(ctx, "docker", "inspect", "--format",
		`{{index .Config.Labels "org.opencontainers.image.source"}}`, image).Output()
	source := ""
	if found := repository.FindStringSubmatch(strings.TrimSpace(string(label))); found != nil {
		source = found[1]
	}
	return image, source, nil
}

// give writes data to name in run, the controller's to read, and mounts it
// read-only at target.
func (c *Controller) give(run, name string, data []byte, target string) ([]string, error) {
	path := filepath.Join(run, name)
	if err := os.WriteFile(path, data, 0o400); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(run)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	if err := c.Owner(root, name); err != nil {
		return nil, err
	}
	return []string{"--mount", readOnly(path, target)}, nil
}

// roots are the roots this host added to its own trust store. Public roots
// are always trusted; these only add to them.
func (c *Controller) roots(run string) ([]string, error) {
	var bundle []byte
	certs, _ := filepath.Glob(filepath.Join(c.CertStore, "*.crt"))
	for _, cert := range certs {
		if data, err := os.ReadFile(cert); err == nil {
			bundle = append(bundle, data...)
		}
	}
	if len(bundle) == 0 {
		return nil, nil
	}
	args, err := c.give(run, "ca.pem", bundle, controllerCA)
	if err != nil {
		return nil, err
	}
	return append(args, "--env", "HQ_CONTROLLER_CA_FILE="+controllerCA), nil
}

// readings are what root reads on the host and hands the container as files:
// each read-only, the controller's to read, and named whether or not it could
// be taken where the controller reports a missing one.
func (c *Controller) readings(ctx context.Context, run string) ([]string, error) {
	var args []string
	give := func(name string, data []byte, target string) error {
		mount, err := c.give(run, name, data, target)
		args = append(args, mount...)
		return err
	}

	roots, err := c.roots(run)
	if err != nil {
		return nil, err
	}
	args = append(args, roots...)

	// The 1Password CLI, lent for the run rather than built into an image the
	// internet-facing web container shares. Absent, the mount is too.
	if op, err := c.LookPath("op"); err == nil {
		args = append(args, "--mount", readOnly(op, "/usr/bin/op"))
	}

	if status, err := Tailnet(ctx, c.Tailscaled, "/localapi/v0/status"); err == nil {
		if err := give("tailnet.json", status, "/run/severino-hq/tailnet.json"); err != nil {
			return nil, err
		}
		args = append(args, "--env", "SEVERINO_TAILNET_STATUS=/run/severino-hq/tailnet.json")
	}
	// Named whether or not the daemon answered, so a status it refused is a
	// failed read in HQ that keeps the last one, rather than no reading at all.
	if lock, err := Tailnet(ctx, c.Tailscaled, "/localapi/v0/tka/status"); err == nil {
		if err := give("tailnet-lock.json", lock, "/run/severino-hq/tailnet-lock.json"); err != nil {
			return nil, err
		}
	}
	args = append(args, "--env", "SEVERINO_TAILNET_LOCK=/run/severino-hq/tailnet-lock.json")

	if chain, err := c.Command(ctx, "nft", "list", "chain", "inet", "host_filter", "input").Output(); err == nil {
		reading, err := ReadFirewall(chain, c.Now())
		if err != nil {
			return nil, err
		}
		if err := give("firewall.json", reading, "/run/severino-hq/firewall.json"); err != nil {
			return nil, err
		}
		args = append(args, "--env", "SEVERINO_HOST_FIREWALL=/run/severino-hq/firewall.json")
	}

	// Each renderer is named whether or not its document is there: one that
	// left none is a finding in HQ, an unnamed one would be silence.
	var named []string
	for _, renderer := range Renderers {
		target := "/run/severino-hq/render-status/" + renderer.Name + ".json"
		named = append(named, renderer.Name+"="+target)
		status := filepath.Join(c.SecretDir, renderer.Status)
		info, err := os.Lstat(status)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		data, err := os.ReadFile(status)
		if err != nil {
			continue
		}
		if err := give("render-status-"+renderer.Name+".json", data, target); err != nil {
			return nil, err
		}
	}
	args = append(args, "--env", "SEVERINO_RENDER_STATUS="+strings.Join(named, ","))

	if shipped, err := Shipped(c.UnitDir()); err == nil {
		if units, err := Reported(c.UnitDir(), shipped); err == nil {
			if state, err := c.UnitState(ctx, units); err == nil && len(state) > 0 {
				if err := give("units", state, "/run/severino-hq/units"); err != nil {
					return nil, err
				}
			}
		}
	}
	args = append(args, "--env", "SEVERINO_HOST_UNITS=/run/severino-hq/units")

	// The last dependency run's outcome, once there has been one.
	if info, err := os.Lstat(c.DependencyRun); err == nil && info.Mode().IsRegular() {
		if data, err := os.ReadFile(c.DependencyRun); err == nil {
			if err := give("dependency-run.json", data, dependencyRunTarget); err != nil {
				return nil, err
			}
			args = append(args, "--env", "SEVERINO_DEPENDENCY_RUN="+dependencyRunTarget)
		}
	}
	return args, nil
}
