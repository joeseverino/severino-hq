//go:build !linux

package install

// SystemMounts refuses: only Linux can show that a directory never pages to
// disk, and the renderer runs nowhere else.
func SystemMounts(string) error {
	return hostError("Controller secret directory must be on tmpfs; that can only be verified on Linux.")
}
