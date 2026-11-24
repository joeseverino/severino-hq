package host

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"
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
