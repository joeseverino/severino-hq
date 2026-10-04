package install

import (
	"os"
	"strconv"
	"strings"
)

// UnprivilegedPortStart is net.ipv4.ip_unprivileged_port_start: the lowest
// port an account without CAP_NET_BIND_SERVICE may listen on.
func UnprivilegedPortStart() (int, error) {
	raw, err := os.ReadFile("/proc/sys/net/ipv4/ip_unprivileged_port_start")
	if err != nil {
		return 0, hostError("net.ipv4.ip_unprivileged_port_start could not be read.")
	}
	floor, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil {
		return 0, hostError("net.ipv4.ip_unprivileged_port_start could not be read.")
	}
	return floor, nil
}
