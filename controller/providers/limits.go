package providers

import "time"

// Policy constants of the providers, each defined once.

const (
	// standardSSHPort is checked on every host perimeter and asked about on
	// every tailnet device, besides the port the connection says it uses.
	standardSSHPort = 22
	// tlsPort is the port every TLS reading is taken on, named because a failed
	// reading reports it.
	tlsPort = 443
	// tlsDialTimeout bounds one TLS handshake reading a served certificate.
	tlsDialTimeout = 15 * time.Second
	// perimeterDialTimeout bounds one "does this port answer" dial.
	perimeterDialTimeout = 3 * time.Second
)

// A renewal's verification policy must fall inside these, in seconds.
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
