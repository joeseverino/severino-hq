package host

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
)

// Install sets the host up for the release its tree holds, as a deploy does
// once HQ is healthy on it: root's verifier first, then the runner image the
// tree pins, verified and pulled, which applies the host's playbook (Setup).
// If the playbook fails, every unit file is put back as it was.
func (d *Deployer) Install(ctx context.Context) (err error) {
	if err := d.Verifier(ctx); err != nil {
		return err
	}
	runner, err := pinnedRunner(d.Lib)
	if err != nil {
		return err
	}
	if err := d.pullVerified(ctx, runner); err != nil {
		return err
	}
	restore, err := d.SnapshotUnits()
	if err != nil {
		return err
	}
	if err := d.Setup(ctx, runner, d.Lib); err != nil {
		return errors.Join(err, restore(), d.run(context.WithoutCancel(ctx), "systemctl", "daemon-reload"))
	}
	fmt.Fprintln(d.Out, "Severino HQ installed, preflighted, and enabled.")
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
	return atomicfile.WriteFile(m.ReleasePath(), []byte(image+"\n"), 0o644)
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
			errs = append(errs, os.MkdirAll(filepath.Dir(target), 0o755), atomicfile.WriteFile(target, data, 0o644))
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
