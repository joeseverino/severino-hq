//go:build !linux

package install

// UnprivilegedPortStart refuses: only Linux says which ports are root's.
func UnprivilegedPortStart() (int, error) {
	return 0, hostError("net.ipv4.ip_unprivileged_port_start can only be read on Linux.")
}
