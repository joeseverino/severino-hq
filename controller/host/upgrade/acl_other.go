//go:build !linux

package upgrade

// hasACL reads no access lists off Linux, where no stack this upgrades runs;
// the drill runs here on the mode bits alone.
func hasACL(string) bool { return false }
