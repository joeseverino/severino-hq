package host

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// TreeItems are what the root-owned tree holds, as paths under /app in the
// image: the configuration root's units read, the units themselves, and the
// programs the image built.
var TreeItems = []string{"hq/config", "deploy"}

// unitPath is a path under the tree that a unit names.
var unitPath = regexp.MustCompile(regexp.QuoteMeta(Lib+"/") + `[A-Za-z0-9_./-]+`)

// Sync replaces the root-owned tree with image's, which must already be
// pulled and verified: nothing is fetched. The copy is made beside the tree,
// made root's and writable by nobody else, refused if a unit names a path it
// does not hold, and swapped in whole.
func (m *Machine) Sync(ctx context.Context, image string) error {
	out, err := m.Command(ctx, "docker", "image", "inspect", "--format", "{{.Id}}", image).Output()
	if err != nil {
		return fmt.Errorf("the image %s is not on this host", image)
	}
	id := strings.TrimSpace(string(out))
	stage, err := os.MkdirTemp(filepath.Dir(m.Lib), ".severino-hq-sync.")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	if err := m.extract(ctx, id, stage); err != nil {
		return err
	}
	if err := seal(stage, m.Own); err != nil {
		return err
	}
	if err := unitsResolve(stage); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(stage, ".source"), []byte("image "+id+"\n"), 0o644); err != nil {
		return err
	}
	at := time.Now().UTC().Format(time.RFC3339)
	if err := os.WriteFile(filepath.Join(stage, ".synced-at"), []byte(at+"\n"), 0o644); err != nil {
		return err
	}
	return swap(stage, m.Lib)
}

// extract copies TreeItems out of image into dir without pulling it again.
func (m *Machine) extract(ctx context.Context, image, dir string) error {
	out, err := m.Command(ctx, "docker", "create", "--pull", "never", image, "true").Output()
	if err != nil {
		return fmt.Errorf("could not open the image %s", image)
	}
	container := strings.TrimSpace(string(out))
	defer func() { _ = m.run(context.WithoutCancel(ctx), "docker", "rm", "--force", container) }()
	for _, item := range TreeItems {
		target := filepath.Join(dir, item)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		if err := m.run(ctx, "docker", "cp", container+":/app/"+item, target); err != nil {
			return fmt.Errorf("the image ships no /app/%s", item)
		}
	}
	return nil
}

// seal makes everything under dir root's, directories and data 0755/0644,
// and executable only what is meant to run: the built programs.
// Links are refused: the tree holds files, and a link would let something
// outside it decide what root runs.
func seal(dir string, own func(string) error) error {
	return filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.Type()&fs.ModeSymlink != 0 {
			return fmt.Errorf("the tree holds a link: %s", strings.TrimPrefix(path, dir+"/"))
		}
		mode := os.FileMode(0o644)
		rel := strings.TrimPrefix(path, dir+"/")
		switch {
		case entry.IsDir():
			mode = 0o755
		case strings.HasPrefix(rel, "deploy/bin/"):
			mode = 0o755
		}
		if err := own(path); err != nil {
			return err
		}
		return os.Chmod(path, mode)
	})
}

// unitsResolve refuses a tree whose units name a path under the tree that it
// does not hold: such a unit fails on its next start.
func unitsResolve(dir string) error {
	units := filepath.Join(dir, "deploy", "systemd")
	return filepath.WalkDir(units, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || strings.HasSuffix(path, ".example") {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		for _, named := range unitPath.FindAllString(string(data), -1) {
			if _, err := os.Stat(filepath.Join(dir, strings.TrimPrefix(named, Lib+"/"))); err != nil {
				return fmt.Errorf("%s names %s, which the tree does not ship", strings.TrimPrefix(path, dir+"/"), named)
			}
		}
		return nil
	})
}

// swap puts stage at lib on the same filesystem, then removes what was there.
func swap(stage, lib string) error {
	old := lib + ".old"
	if err := os.RemoveAll(old); err != nil {
		return err
	}
	if err := os.Rename(lib, old); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := os.Rename(stage, lib); err != nil {
		_ = os.Rename(old, lib)
		return err
	}
	return os.RemoveAll(old)
}
