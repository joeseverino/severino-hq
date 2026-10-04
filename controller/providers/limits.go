package providers

import "time"

// Policy constants of the providers, each defined once. Where Python's
// controller_runtime holds the same value its source is cited.

const (
	// standardSSHPort is checked on every host perimeter and asked about on
	// every tailnet device, besides the port the connection says it uses.
	standardSSHPort = 22
	// tlsPort is the port every TLS reading is taken on, named because a failed
	// reading reports it (tls_verification.py TLS_PORT).
	tlsPort = 443
	// tlsDialTimeout bounds one TLS handshake reading a served certificate
	// (tls_verification.py, timeout=15).
	tlsDialTimeout = 15 * time.Second
	// perimeterDialTimeout bounds one "does this port answer" dial
	// (host_readings.py _answers_from_here, timeout=3.0).
	perimeterDialTimeout = 3 * time.Second
)

// A renewal's verification policy must fall inside these, in seconds
// (tls_verification.py, 30 <= timeout <= 600 and 1 <= interval <= 30).
const (
	verificationTimeoutMin  = 30
	verificationTimeoutMax  = 600
	verificationIntervalMin = 1
	verificationIntervalMax = 30
)

// AdGuard's query log is read a page of adguardQueryPage entries at a time;
// a short page is the last one, so the request and the test share the number.
const adguardQueryPage = 500

// adguardQueryWindow is how far back the query log is read.
const adguardQueryWindow = 24 * time.Hour
