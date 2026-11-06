package host

import (
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"os"
	"path/filepath"
)

// Rendered reports whether dir holds the application environment as the
// renderer installs it: a directory only root can enter, holding one regular
// file of the web account's with no other name. Anything else (missing, a
// link, a directory docker made at the file's name) is not it, and says why.
func Rendered(dir string, rootUID, webUID int) error {
	info, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("%s is not there yet", dir)
	}
	if uid, _, ok := hostcore.Ownership(info); !ok || !info.IsDir() || uid != rootUID || info.Mode().Perm() != 0o700 {
		return fmt.Errorf("%s is not a directory only root can enter", dir)
	}
	file := filepath.Join(dir, hostcore.AppEnvName)
	info, err = os.Lstat(file)
	if err != nil {
		return fmt.Errorf("%s is not rendered yet", file)
	}
	if uid, links, ok := hostcore.Ownership(info); !ok || !info.Mode().IsRegular() || uid != webUID || links != 1 {
		return fmt.Errorf("%s is not the file the renderer installs", file)
	}
	return nil
}
