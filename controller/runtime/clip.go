package runtime

import "github.com/joeseverino/severino-hq/controller/api"

// Limits on text that crosses into a report, a log line or an error, cut by
// characters.
const (
	ReportTextLimit = 500 // a failure's text in a report
	ReasonLimit     = 200 // why one optional read or part was refused
	VerdictLimit    = 300 // a remote verdict quoted in an error
	SaidLimit       = 240 // the last line a child printed
	isoDateLength   = 10  // YYYY-MM-DD
)

// AnalyticsValueMax is the longest analytics dimension value the contract
// accepts.
var AnalyticsValueMax = api.MustLimit("AnalyticsRow", "properties", "value", "maxLength")

// Clip cuts s to at most n characters. It cuts between characters, never
// inside a multi-byte one.
func Clip(s string, n int) string {
	if n < 0 || len(s) <= n {
		return s
	}
	if runes := []rune(s); len(runes) > n {
		return string(runes[:n])
	}
	return s
}

// ReportText is failure text as a report carries it.
func ReportText(s string) string { return Clip(s, ReportTextLimit) }

// ISODate is the date part of an ISO-8601 timestamp.
func ISODate(s string) string { return Clip(s, isoDateLength) }
