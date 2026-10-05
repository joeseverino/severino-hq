package runtime

import (
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
)

// Timeouts and intervals, each defined once.
const (
	// DefaultRequestTimeout bounds one provider HTTP call.
	DefaultRequestTimeout = 15 * time.Second
	// MultipartTimeout bounds a certificate upload, which carries key material
	// and is slower to answer.
	MultipartTimeout = 30 * time.Second
	// BridgeTimeout bounds one call into Django.
	BridgeTimeout = 3 * time.Minute
	// CommandTimeout bounds one local tool or SSH call.
	CommandTimeout = 180 * time.Second
	// SSHConnectTimeoutSeconds is ssh's ConnectTimeout, in the seconds ssh takes.
	SSHConnectTimeoutSeconds = 10
	// SlowSweep is how long a sweep runs before it is worth a line naming the
	// slowest readers.
	SlowSweep = 60 * time.Second
)

// MaxBridgeOutput bounds one bridge message in either direction: a payload
// over it is not sent and an answer over it is refused. The contract states
// it, and HQ's bridge application enforces the same number.
var MaxBridgeOutput = api.MustLimit("BridgeBody", "maxLength")

// ClaimLeaseSeconds is how long a claimed operation is leased: the contract's
// default, which HQ applies to a claim that names none.
var ClaimLeaseSeconds = api.MustLimit("LeaseSeconds", "default")
