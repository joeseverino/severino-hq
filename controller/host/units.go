package host

import (
	"bufio"
	"bytes"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
)

// unitSuffixes are the unit types systemd reads from a unit directory.
var unitSuffixes = []string{".service", ".socket", ".mount", ".automount", ".swap", ".target", ".path", ".timer", ".slice"}

func isUnit(name string) bool {
	return slices.ContainsFunc(unitSuffixes, func(suffix string) bool { return strings.HasSuffix(name, suffix) })
}

// Shipped is every file under dir that systemd would read, relative to dir and
// sorted: a unit at the top level, or a .conf drop-in one directory down.
// That is systemd's rule, so a unit added to the repository is in the set
// with nothing here changing. Templates (*.example) are a person's to copy.
func Shipped(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("no unit directory at %s", dir)
	}
	var shipped []string
	for _, entry := range entries {
		name := entry.Name()
		switch {
		case entry.Type().IsRegular() && isUnit(name):
			shipped = append(shipped, name)
		case entry.IsDir() && strings.HasSuffix(name, ".d"):
			dropins, err := os.ReadDir(filepath.Join(dir, name))
			if err != nil {
				return nil, err
			}
			for _, dropin := range dropins {
				if dropin.Type().IsRegular() && strings.HasSuffix(dropin.Name(), ".conf") {
					shipped = append(shipped, path.Join(name, dropin.Name()))
				}
			}
		}
	}
	slices.Sort(shipped)
	return shipped, nil
}

// WithDropIns is shipped's drop-ins and the named units, to install together.
// A drop-in reaches every unit its directory's name covers, a prefix
// directory (severino-hq-.service.d) every unit under it, so a unit installed
// before them would start under its new file and the last release's sandbox,
// where the new file leaves that sandbox to a drop-in.
func WithDropIns(shipped []string, units ...string) []string {
	return slices.DeleteFunc(slices.Clone(shipped), func(name string) bool {
		return !strings.Contains(name, "/") && !slices.Contains(units, name)
	})
}

// Retired is every unit in installed that carries this repository's prefix
// and is not shipped: what an earlier release installed and this one dropped.
// Drop-ins beside a shipped unit are the host's to own and are not listed.
func Retired(shipped []string, installed string) ([]string, error) {
	entries, err := os.ReadDir(installed)
	if err != nil {
		return nil, err
	}
	var retired []string
	for _, entry := range entries {
		name := entry.Name()
		if entry.Type().IsRegular() && strings.HasPrefix(name, "severino-hq") && isUnit(name) && !slices.Contains(shipped, name) {
			retired = append(retired, name)
		}
	}
	return retired, nil
}

// Enabled is the shipped units to enable: every timer and path, which start
// work, and every other unit with an [Install] section, which starts at boot.
func Enabled(dir string, shipped []string) ([]string, error) {
	var enabled []string
	for _, name := range shipped {
		if strings.Contains(name, "/") {
			continue
		}
		if strings.HasSuffix(name, ".timer") || strings.HasSuffix(name, ".path") {
			enabled = append(enabled, name)
			continue
		}
		installs, err := hasInstallSection(filepath.Join(dir, name))
		if err != nil {
			return nil, err
		}
		if installs {
			enabled = append(enabled, name)
		}
	}
	return enabled, nil
}

func hasInstallSection(file string) (bool, error) {
	data, err := os.ReadFile(file)
	if err != nil {
		return false, err
	}
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		if strings.TrimSpace(scanner.Text()) == "[Install]" {
			return true, nil
		}
	}
	return false, scanner.Err()
}

// InstallUnit copies shipped file name from dir into the systemd directory,
// root-owned and writable by nobody else, making its drop-in directory first.
func InstallUnit(dir, systemd, name string) error {
	data, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		return err
	}
	target := filepath.Join(systemd, name)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	return atomicfile.WriteFile(target, data, 0o644)
}
