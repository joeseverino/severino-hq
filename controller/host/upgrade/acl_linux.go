package upgrade

import "golang.org/x/sys/unix"

// hasACL is whether path carries a POSIX access list, which `ls -l` marks
// with a "+".
func hasACL(path string) bool {
	for _, name := range []string{"system.posix_acl_access", "system.posix_acl_default"} {
		if size, err := unix.Lgetxattr(path, name, nil); err == nil && size > 0 {
			return true
		}
	}
	return false
}
