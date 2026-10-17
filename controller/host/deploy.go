package host

import (
	"bufio"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

// BuildSource is the repository this program was built from, owner/name,
// set by the image build from the source it labels the image with. What a
// deploy accepts is derived from it, so a fork deploys its own images and a
// build that names no source deploys nothing.
var BuildSource string

const signerIssuer = "https://token.actions.githubusercontent.com"

var sourceNamed = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)

// Source is a repository a deploy accepts images from.
type Source string

// Composition is the source's composed image, which its compose workflow
// signs. A different repository would be someone else's code.
func (s Source) Composition() string { return "ghcr.io/" + strings.ToLower(string(s)) + "/composition" }

// Signer is the certificate identity a deployable image is signed with: the
// source's compose workflow, on main.
func (s Source) Signer() string {
	return `^https://github\.com/` + regexp.QuoteMeta(string(s)) + `/\.github/workflows/compose\.yml@refs/heads/main$`
}

// Deployable returns image when it is a digest-pinned reference to the
// source's composition. The sudoers rule lets the caller choose this
// argument, and it decides what root runs as a container.
func (s Source) Deployable(image string) (string, error) {
	if !sourceNamed.MatchString(string(s)) {
		return "", errors.New("this build names no source repository, so it deploys nothing")
	}
	if !regexp.MustCompile(`^` + regexp.QuoteMeta(s.Composition()) + `@sha256:[0-9a-f]{64}$`).MatchString(image) {
		return "", fmt.Errorf("refusing to deploy %q: not a digest-pinned reference to %s", image, s.Composition())
	}
	return image, nil
}

// Deployer holds what a deploy needs beyond the machine.
type Deployer struct {
	*Machine
	// Source is the repository whose signed composition is deployable.
	Source Source
	// Cosign is root's own verifier, outside the tree a deploy replaces.
	Cosign string
	// Exec replaces this process with another program: the release's own
	// hq-host. A test records the call instead.
	Exec func(argv0 string, argv, env []string) error
	// Drain bounds the wait for a run that started before the deploy.
	Drain, Poll time.Duration
	// FreeBytes is the floor of free space a deploy needs on FreeOn, the
	// filesystem docker keeps images on.
	FreeBytes uint64
	FreeOn    string
	// Free reports the free bytes under a path.
	Free func(path string) (uint64, error)
}

// Deploy is the first half, run by the release already on the host: it
// verifies and pulls the image, then hands over to the image's own hq-host,
// so the release being installed is the one that installs it.
// credentials holds "user\ntoken\n", or nothing for a host with its own.
func (d *Deployer) Deploy(ctx context.Context, image string, credentials io.Reader) error {
	image, err := d.Source.Deployable(image)
	if err != nil {
		return err
	}
	// Everything this deploy stages lives here, the registry credential with
	// it, and the release stage removes it whether it succeeds or not.
	stage, err := os.MkdirTemp(d.Runtime, "severino-hq-release.")
	if err != nil {
		return err
	}
	cleanup := func() { os.RemoveAll(stage) }
	config, err := d.registryConfig(stage, credentials)
	if err != nil {
		cleanup()
		return err
	}
	env := []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
	if config != "" {
		env = append(env, "DOCKER_CONFIG="+config)
		d.Command = withEnv(d.Command, env)
	}
	// The signature is checked here, by root, and nowhere before it: the
	// runner that asked for this deploy is the party the sudoers rule
	// distrusts, and the shape above is exactly what a stolen registry token
	// could also push. The reference is pinned by digest, so what is verified
	// is what is pulled and run.
	if err := d.run(ctx, d.Cosign, "verify",
		"--certificate-oidc-issuer", signerIssuer,
		"--certificate-identity-regexp", d.Source.Signer(), image); err != nil {
		cleanup()
		return fmt.Errorf("refusing to deploy %s: not signed by this repository's compose workflow", image)
	}
	fmt.Fprintln(d.Out, "Signature verified.")
	// Read before the prune below, which can remove the running image when
	// HQ is stopped.
	running, err := d.runningExtensions(ctx, stage)
	if err != nil {
		cleanup()
		return fmt.Errorf("refusing to deploy %s: %w", image, err)
	}
	// The running image is referenced and survives, so it stays the rollback.
	_ = d.run(ctx, "docker", "image", "prune", "--all", "--force")
	_ = d.run(ctx, "docker", "builder", "prune", "--all", "--force")
	if free, err := d.Free(d.FreeOn); err == nil && free < d.FreeBytes {
		cleanup()
		return fmt.Errorf("a deploy needs %d MiB free; %d MiB are", d.FreeBytes>>20, free>>20)
	}
	if err := d.private(ctx, "Image pull", "pull", "docker", "pull", image); err != nil {
		cleanup()
		return err
	}
	if err := d.keepsExtensions(ctx, stage, image, running); err != nil {
		cleanup()
		return err
	}
	next := filepath.Join(stage, "hq-host")
	// --pull never: what is copied out is the image verified above, or nothing.
	if err := d.copyOut(ctx, image, "/app/deploy/bin/hq-host", next); err != nil {
		cleanup()
		return err
	}
	if err := d.Exec(next, []string{next, "release", "--stage", stage, image}, env); err != nil {
		cleanup()
		return fmt.Errorf("could not hand over to the release's hq-host: %w", err)
	}
	return nil
}

// Release is the second half, run from the image being deployed. It
// replaces the root-owned tree with the image's and installs the image's web
// unit before restarting HQ, so HQ comes up as the new release defines it;
// once HQ is healthy it installs the rest. If any of it fails, the previous
// image, tree and units are put back and HQ restarted on them.
func (d *Deployer) Release(ctx context.Context, image string) (err error) {
	image, err = d.Source.Deployable(image)
	if err != nil {
		return err
	}
	previous, err := os.ReadFile(d.ReleasePath())
	if err != nil {
		return fmt.Errorf("no release is recorded to roll back to: %w", err)
	}
	held, err := d.hold(ctx)
	if err != nil {
		d.restart(held)
		return err
	}
	units, err := d.SnapshotUnits()
	if err != nil {
		d.restart(held)
		return err
	}
	tree, err := d.backupTree()
	if err != nil {
		d.restart(held)
		return err
	}
	defer os.RemoveAll(tree)
	kept, err := d.schemaSnapshot(ctx)
	if err != nil {
		d.restart(held)
		return err
	}
	// One left by an earlier release is not the database this one finds.
	if err := os.Remove(kept); err != nil && !errors.Is(err, os.ErrNotExist) {
		d.restart(held)
		return err
	}
	defer func() {
		if err != nil {
			err = errors.Join(err, d.rollBack(strings.TrimSpace(string(previous)), tree, units, kept, held))
		}
	}()

	if err := d.Sync(ctx, image); err != nil {
		return err
	}
	fmt.Fprintln(d.Out, "Tree synced from the image.")
	if err := d.installWebUnit(ctx); err != nil {
		return err
	}
	if err := d.Record(image); err != nil {
		return err
	}
	// Returns once HQ answers its health check on the new image, or fails.
	if err := d.run(ctx, "systemctl", "restart", WebUnit); err != nil {
		_ = d.private(context.WithoutCancel(ctx), "Application failure diagnostics", "application-failure",
			"journalctl", "--unit", WebUnit, "--lines", "80", "--no-pager")
		return fmt.Errorf("the new image did not become healthy: %w", err)
	}
	fmt.Fprintln(d.Out, "HQ is healthy on the new image.")
	if err := d.private(ctx, "Release install", "release-install", filepath.Join(d.Lib, "deploy", "bin", "hq-host"), "install"); err != nil {
		return err
	}
	d.restart(held)
	// The release stands, so the copy is not needed; the backups hold that state.
	if err := os.Remove(kept); err != nil && !errors.Is(err, os.ErrNotExist) {
		fmt.Fprintf(d.Out, "The database kept before migrating is still at %s: %v\n", kept, err)
	}
	fmt.Fprintf(d.Out, "Deployed %s.\n", image)
	return nil
}

// installWebUnit installs the synced tree's web unit and its drop-ins.
func (d *Deployer) installWebUnit(ctx context.Context) error {
	shipped, err := Shipped(d.UnitDir())
	if err != nil {
		return err
	}
	for _, name := range shipped {
		if name == WebUnit || strings.HasPrefix(name, WebUnit+".d/") {
			if err := InstallUnit(d.UnitDir(), d.Systemd, name); err != nil {
				return err
			}
		}
	}
	return d.run(ctx, "systemctl", "daemon-reload")
}

func (d *Deployer) rollBack(previous, tree string, units func() error, kept string, held []string) error {
	ctx := context.Background()
	fmt.Fprintf(d.Out, "Restoring %s.\n", previous)
	errs := []error{d.Record(previous), d.restoreTree(tree), units(), d.run(ctx, "systemctl", "daemon-reload")}
	// The previous image knows only the schema it was built with, so the
	// database goes back first; an error here leaves HQ stopped, not running
	// on a schema it cannot read.
	if err := d.restoreSchema(ctx, kept); err != nil {
		errs = append(errs, err)
	} else {
		errs = append(errs, d.run(ctx, "systemctl", "restart", WebUnit))
	}
	d.restart(held)
	return errors.Join(errs...)
}

// restoreSchema puts back the database a boot of the failed release kept
// before migrating it. What that release wrote after its boot is discarded:
// it never became healthy.
func (d *Deployer) restoreSchema(ctx context.Context, kept string) error {
	if _, err := os.Stat(kept); errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err := d.run(ctx, "systemctl", "stop", WebUnit); err != nil {
		return fmt.Errorf("could not stop HQ to restore its database: %w", err)
	}
	database := filepath.Join(filepath.Dir(kept), Database)
	if err := os.Rename(kept, database); err != nil {
		return err
	}
	// The log of the migrated database is not this one's.
	for _, log := range []string{database + "-wal", database + "-shm"} {
		if err := os.Remove(log); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	fmt.Fprintln(d.Out, "Restored the database as it was before the release migrated it.")
	return nil
}

// hold stops every timer and path unit the tree ships that is active, so
// nothing starts against a half-replaced release, and waits for the runs
// they already started. It returns what it stopped.
func (d *Deployer) hold(ctx context.Context) ([]string, error) {
	shipped, err := Shipped(d.UnitDir())
	if err != nil {
		return nil, err
	}
	var held []string
	for _, name := range shipped {
		if (strings.HasSuffix(name, ".timer") || strings.HasSuffix(name, ".path")) && d.active(ctx, name) {
			held = append(held, name)
		}
	}
	if len(held) > 0 {
		_ = d.run(ctx, "systemctl", append([]string{"stop"}, held...)...)
	}
	deadline := time.Now().Add(d.Drain)
	for time.Now().Before(deadline) {
		if !d.active(ctx, ControllerUnit) && !d.active(ctx, SecretsUnit) {
			return held, nil
		}
		select {
		case <-ctx.Done():
			return held, ctx.Err()
		case <-time.After(d.Poll):
		}
	}
	fmt.Fprintln(d.Out, "A run started before the deploy is still going; it fails against the new image and the next one retries.")
	return held, nil
}

func (d *Deployer) restart(held []string) {
	for _, name := range held {
		_ = d.run(context.Background(), "systemctl", "start", name)
	}
}

func (d *Deployer) backupTree() (string, error) {
	backup, err := os.MkdirTemp(d.Runtime, "severino-hq-tree.")
	if err != nil {
		return "", err
	}
	if err := d.run(context.Background(), "cp", "-a", d.Lib+"/.", backup+"/"); err != nil {
		os.RemoveAll(backup)
		return "", err
	}
	return backup, nil
}

func (d *Deployer) restoreTree(backup string) error {
	ctx := context.Background()
	staged := d.Lib + ".restore"
	os.RemoveAll(staged)
	if err := d.run(ctx, "cp", "-a", backup, staged); err != nil {
		return err
	}
	old := d.Lib + ".rolled-back"
	os.RemoveAll(old)
	if err := os.Rename(d.Lib, old); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Rename(staged, d.Lib); err != nil {
		return err
	}
	return os.RemoveAll(old)
}

// copyOut copies one file out of image without pulling it again.
func (d *Deployer) copyOut(ctx context.Context, image, from, to string) error {
	out, err := d.Command(ctx, "docker", "create", "--pull", "never", image, "true").Output()
	if err != nil {
		return fmt.Errorf("could not read %s from the image: %w", from, err)
	}
	container := strings.TrimSpace(string(out))
	defer d.run(context.WithoutCancel(ctx), "docker", "rm", "--force", container)
	if err := d.run(ctx, "docker", "cp", container+":"+from, to); err != nil {
		return fmt.Errorf("the image ships no %s: %w", from, err)
	}
	return os.Chmod(to, 0o700)
}

// registryConfig writes the registry credential, if one was given, to a
// private directory for this deploy alone. It arrives on stdin, never as an
// argument or variable, which the process table and sudo would expose.
func (d *Deployer) registryConfig(stage string, credentials io.Reader) (string, error) {
	if credentials == nil {
		return "", nil
	}
	lines := bufio.NewScanner(io.LimitReader(credentials, 8<<10))
	var user, token string
	if lines.Scan() {
		user = strings.TrimSpace(lines.Text())
	}
	if lines.Scan() {
		token = strings.TrimSpace(lines.Text())
	}
	if token == "" {
		return "", nil
	}
	if user == "" {
		user = "x-access-token"
	}
	dir := filepath.Join(stage, "docker")
	if err := os.Mkdir(dir, 0o700); err != nil {
		return "", err
	}
	auth := base64.StdEncoding.EncodeToString([]byte(user + ":" + token))
	config := `{"auths":{"ghcr.io":{"auth":"` + auth + `"}}}` + "\n"
	if err := os.WriteFile(filepath.Join(dir, "config.json"), []byte(config), 0o600); err != nil {
		return "", err
	}
	return dir, nil
}

func withEnv(build func(context.Context, string, ...string) *exec.Cmd, env []string) func(context.Context, string, ...string) *exec.Cmd {
	return func(ctx context.Context, name string, args ...string) *exec.Cmd {
		command := build(ctx, name, args...)
		command.Env = append(command.Env, env...)
		return command
	}
}

func copyFile(from, to string, mode os.FileMode) error {
	data, err := os.ReadFile(from)
	if err != nil {
		return err
	}
	return writeAtomic(to, data, mode)
}

// Free is the free bytes on the filesystem holding path.
func Free(path string) (uint64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, err
	}
	return stat.Bavail * uint64(stat.Bsize), nil
}
