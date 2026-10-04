package runtime

import "time"

// Timeouts and intervals, each defined once. The Python controller reads none
// of them from configuration either; its source is cited so the two stay equal
// until cutover.
const (
	// DefaultRequestTimeout bounds one provider HTTP call
	// (controller_runtime/provider_http.py open_url, timeout=15).
	DefaultRequestTimeout = 15 * time.Second
	// MultipartTimeout bounds a certificate upload, which carries key material
	// and is slower to answer.
	MultipartTimeout = 30 * time.Second
	// BridgeTimeout bounds one call into Django.
	BridgeTimeout = 3 * time.Minute
	// CommandTimeout bounds one local tool or SSH call
	// (controller_runtime/commands.py, timeout=180).
	CommandTimeout = 180 * time.Second
	// SSHConnectTimeoutSeconds is ssh's ConnectTimeout, in the seconds ssh takes.
	SSHConnectTimeoutSeconds = 10
	// SlowSweep is how long a sweep runs before it is worth a line naming the
	// slowest readers.
	SlowSweep = 60 * time.Second
)

// MaxBridgeOutput bounds what the bridge may print before the call is refused.
const MaxBridgeOutput = 64 << 20
