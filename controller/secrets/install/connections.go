package install

import (
	"os"
	"path/filepath"
)

// RequireConnections reports whether dir holds the connections document as
// the renderer leaves it: a directory only root can enter, on a tmpfs that
// never pages to disk, holding a nonempty file only root can read. Whatever
// reads the document refuses it otherwise.
func RequireConnections(dir string, mounts MountChecker) error {
	info, err := os.Lstat(dir)
	if err != nil {
		return hostError("Controller secret directory must be root-owned with mode 0700.")
	}
	if uid, _, ok := owner(info); !ok || !info.IsDir() || uid != 0 || info.Mode().Perm() != 0o700 {
		return hostError("Controller secret directory must be root-owned with mode 0700.")
	}
	if err := mounts(dir); err != nil {
		return err
	}
	info, err = os.Lstat(filepath.Join(dir, ConnectionsName))
	if err != nil {
		return hostError("Controller connections must be a nonempty root-owned file with mode 0400.")
	}
	if uid, _, ok := owner(info); !ok || !info.Mode().IsRegular() || info.Size() == 0 || uid != 0 || info.Mode().Perm() != 0o400 {
		return hostError("Controller connections must be a nonempty root-owned file with mode 0400.")
	}
	return nil
}
