package host

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// Install puts the release in Lib on the host: its units, its tmpfiles rule
// and its verifier. The renderer's units go first and alone, because what
// follows consumes what it renders. If the install fails, every unit file is
// put back as it was. Enabling severino-hq-web.service is what moves a host
// from a compose-run container to the unit: the unit replaces the container
// that holds HQ's name with its own, and is not enabled unless HQ comes up.
//
// connections checks the controller's connections document once rendered.
func (m *Machine) Install(ctx context.Context, connections func() error) (err error) {
	dir := m.UnitDir()
	shipped, err := Shipped(dir)
	if err != nil {
		return err
	}
	if err := m.Verifier(ctx); err != nil {
		return err
	}
	var units []string
	for _, name := range shipped {
		if !strings.Contains(name, "/") {
			units = append(units, filepath.Join(dir, name))
		}
	}
	if err := m.private(ctx, "Unit verification", "unit-verify", "systemd-analyze", append([]string{"verify"}, units...)...); err != nil {
		return err
	}

	var render, rest []string
	for _, name := range shipped {
		if name == SecretsUnit || strings.HasPrefix(name, SecretsUnit+".d/") {
			render = append(render, name)
		} else {
			rest = append(rest, name)
		}
	}
	restore, err := m.SnapshotUnits()
	if err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			err = errors.Join(err, restore(), m.run(context.WithoutCancel(ctx), "systemctl", "daemon-reload"))
		}
	}()
	for _, name := range render {
		if err := InstallUnit(dir, m.Systemd, name); err != nil {
			return err
		}
	}
	if err := m.run(ctx, "systemctl", "daemon-reload"); err != nil {
		return err
	}
	if err := m.run(ctx, "systemctl", "start", SecretsUnit); err != nil {
		return fmt.Errorf("the render failed: %w", err)
	}
	if err := connections(); err != nil {
		return err
	}
	if err := m.private(ctx, "Controller connection preflight", "controller-preflight", filepath.Join(m.Lib, "deploy", "bin", "hq-host"), "controller"); err != nil {
		return err
	}
	if err := m.private(ctx, "Content index preflight", "content-index-preflight", "docker", "exec", Container, "hq-controller", "job", "content.sync"); err != nil {
		return err
	}

	for _, name := range rest {
		if err := InstallUnit(dir, m.Systemd, name); err != nil {
			return err
		}
	}
	retired, err := Retired(shipped, m.Systemd)
	if err != nil {
		return err
	}
	for _, name := range retired {
		_ = m.run(ctx, "systemctl", "disable", "--now", name)
		if err := os.Remove(filepath.Join(m.Systemd, name)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	if err := m.run(ctx, "systemctl", "daemon-reload"); err != nil {
		return err
	}
	if err := m.takeOver(ctx); err != nil {
		return err
	}
	enabled, err := Enabled(dir, shipped)
	if err != nil {
		return err
	}
	if err := m.run(ctx, "systemctl", append([]string{"enable", "--now"}, enabled...)...); err != nil {
		return err
	}
	if err := m.tidy(shipped); err != nil {
		return err
	}
	committed = true
	fmt.Fprintln(m.Out, "Severino HQ installed, preflighted, and enabled.")
	return nil
}

// takeOver starts severino-hq-web.service on a host where it is not running
// yet, recording the image the running container was deployed with as the
// release: what is running is the truth, and a record left by an attempt
// that failed is not. A start that fails leaves the unit disabled and HQ's
// name free, so the deploy that called this can put its own container back.
func (m *Machine) takeOver(ctx context.Context) error {
	if m.active(ctx, WebUnit) {
		return nil
	}
	out, err := m.Command(ctx, "docker", "inspect", "--format", "{{.Config.Image}}", Container).Output()
	switch {
	case err == nil:
		image, err := Pinned(strings.TrimSpace(string(out)))
		if err != nil {
			return err
		}
		if err := m.Record(image); err != nil {
			return err
		}
	default:
		if _, statErr := os.Stat(m.ReleasePath()); statErr != nil {
			return fmt.Errorf("no container is running and no release is recorded: %w", err)
		}
	}
	if err := m.run(ctx, "systemctl", "enable", "--now", WebUnit); err != nil {
		ctx := context.WithoutCancel(ctx)
		_ = m.run(ctx, "systemctl", "disable", "--now", WebUnit)
		_ = m.run(ctx, "docker", "rm", "--force", Container)
		return fmt.Errorf("HQ did not come up under its unit: %w", err)
	}
	fmt.Fprintln(m.Out, "HQ runs under "+WebUnit+".")
	return nil
}

// Record writes image as the release the web unit runs.
func (m *Machine) Record(image string) error {
	if _, err := Pinned(image); err != nil {
		return err
	}
	if err := os.MkdirAll(m.State, 0o755); err != nil {
		return err
	}
	return writeAtomic(m.ReleasePath(), []byte(image+"\n"), 0o644)
}

// SnapshotUnits saves every unit file and drop-in of this repository's in
// the systemd directory, and returns what puts the directory back as it was:
// each saved file restored, each one added since removed.
func (m *Machine) SnapshotUnits() (func() error, error) {
	saved, err := m.installedUnits()
	if err != nil {
		return nil, err
	}
	return func() error {
		now, err := m.installedUnits()
		if err != nil {
			return err
		}
		var errs []error
		for name := range now {
			if _, kept := saved[name]; !kept {
				errs = append(errs, os.Remove(filepath.Join(m.Systemd, name)))
			}
		}
		for name, data := range saved {
			target := filepath.Join(m.Systemd, name)
			errs = append(errs, os.MkdirAll(filepath.Dir(target), 0o755), writeAtomic(target, data, 0o644))
		}
		return errors.Join(errs...)
	}, nil
}

// installedUnits reads every severino-hq unit and drop-in installed.
func (m *Machine) installedUnits() (map[string][]byte, error) {
	units := map[string][]byte{}
	entries, err := os.ReadDir(m.Systemd)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, "severino-hq") {
			continue
		}
		if entry.Type().IsRegular() {
			data, err := os.ReadFile(filepath.Join(m.Systemd, name))
			if err != nil {
				return nil, err
			}
			units[name] = data
			continue
		}
		if !entry.IsDir() {
			continue
		}
		dropins, err := os.ReadDir(filepath.Join(m.Systemd, name))
		if err != nil {
			return nil, err
		}
		for _, dropin := range dropins {
			if dropin.Type().IsRegular() {
				data, err := os.ReadFile(filepath.Join(m.Systemd, name, dropin.Name()))
				if err != nil {
					return nil, err
				}
				units[filepath.Join(name, dropin.Name())] = data
			}
		}
	}
	return units, nil
}

// tidy removes what earlier releases left that nothing reads, and installs
// the tmpfiles rule that makes the doorbell directory at every boot.
func (m *Machine) tidy(shipped []string) error {
	for _, leftover := range []string{
		filepath.Join(m.Runtime, filepath.Base(RunDir), "severino_controller_env"),
		filepath.Join(m.Runtime, filepath.Base(SecretDir), "severino_controller_env"),
	} {
		if err := os.Remove(leftover); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	if err := os.RemoveAll(filepath.Join(m.Runtime, "severino-hq-op")); err != nil {
		return err
	}
	// What earlier releases installed that this one does not: the shell sync
	// program, and drop-ins that restated each unit's own ExecStart. A name
	// this release ships is its own again and is left alone.
	if err := os.Remove(filepath.Join(m.Sbin, "severino-hq-sync-scripts")); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	for _, unit := range []string{"severino-hq-backup.service", ControllerUnit, SecretsUnit} {
		dropin := unit + ".d/10-root-owned-exec.conf"
		if slices.Contains(shipped, dropin) {
			continue
		}
		if err := os.Remove(filepath.Join(m.Systemd, dropin)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	rule, err := os.ReadFile(filepath.Join(m.Lib, "deploy", "tmpfiles", "severino-hq.conf"))
	if err != nil {
		return err
	}
	if err := os.MkdirAll(m.Tmpfiles, 0o755); err != nil {
		return err
	}
	target := filepath.Join(m.Tmpfiles, "severino-hq.conf")
	if err := writeAtomic(target, rule, 0o644); err != nil {
		return err
	}
	return m.run(context.Background(), "systemd-tmpfiles", "--create", target)
}
