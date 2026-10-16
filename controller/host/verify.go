package host

import (
	"bytes"
	"context"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// Verify reports where the host differs from the release HQ runs: the tree
// against that image, file for file and byte for byte; the installed units
// against the tree's; and anything a release no longer installs. It changes
// nothing. An empty answer is a host that is what its release says.
func (m *Machine) Verify(ctx context.Context) ([]string, error) {
	out, err := m.Command(ctx, "docker", "inspect", "--format", "{{.Image}}", Container).Output()
	if err != nil {
		return nil, fmt.Errorf("could not read the image HQ runs")
	}
	running := strings.TrimSpace(string(out))
	var drift []string

	source, _ := os.ReadFile(filepath.Join(m.Lib, ".source"))
	if recorded := strings.TrimPrefix(strings.TrimSpace(string(source)), "image "); recorded != running {
		drift = append(drift, fmt.Sprintf("the tree came from %q; HQ runs %s", recorded, running))
	}

	stage, err := os.MkdirTemp(m.Runtime, "severino-hq-verify.")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(stage)
	if err := m.extract(ctx, running, stage); err != nil {
		return nil, err
	}
	differs, err := compareTrees(stage, m.Lib)
	if err != nil {
		return nil, err
	}
	drift = append(drift, differs...)

	shipped, err := Shipped(m.UnitDir())
	if err != nil {
		return nil, err
	}
	for _, name := range shipped {
		want, _ := os.ReadFile(filepath.Join(m.UnitDir(), name))
		have, err := os.ReadFile(filepath.Join(m.Systemd, name))
		if err != nil || !bytes.Equal(want, have) {
			drift = append(drift, fmt.Sprintf("%s is not the release's", filepath.Join(m.Systemd, name)))
		}
	}
	retired, err := Retired(shipped, m.Systemd)
	if err != nil {
		return nil, err
	}
	for _, name := range retired {
		drift = append(drift, fmt.Sprintf("%s is not shipped by the release", filepath.Join(m.Systemd, name)))
	}
	leftovers, _ := filepath.Glob(filepath.Join(m.Sbin, "severino-hq-*"))
	for _, leftover := range leftovers {
		drift = append(drift, fmt.Sprintf("%s is installed by no release", leftover))
	}
	return drift, nil
}

// compareTrees names every file that is in one tree and not the other, or
// in both with different bytes. The tree's own provenance files are not the
// image's and are not compared.
func compareTrees(want, have string) ([]string, error) {
	wanted, err := files(want)
	if err != nil {
		return nil, err
	}
	had, err := files(have)
	if err != nil {
		return nil, err
	}
	var drift []string
	for name, data := range wanted {
		switch held, ok := had[name]; {
		case !ok:
			drift = append(drift, name+" is missing from the tree")
		case !bytes.Equal(data, held):
			drift = append(drift, name+" differs from the image's")
		}
	}
	for name := range had {
		if _, ok := wanted[name]; !ok && name != ".source" && name != ".synced-at" {
			drift = append(drift, name+" is not the image's")
		}
	}
	slices.Sort(drift)
	return drift, nil
}

func files(dir string) (map[string][]byte, error) {
	found := map[string][]byte{}
	err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		found[strings.TrimPrefix(path, dir+"/")] = data
		return nil
	})
	return found, err
}
