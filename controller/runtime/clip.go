package runtime

// Limits on text that crosses into a report, a log line or an error. Python's
// controller_runtime and control_plane/provider_adapters/parts.py hold the
// same numbers, cut by characters as Python slices a str.
const (
	ReportTextLimit   = 500 // a failure's text in a report (parts.REPORT_TEXT_LIMIT)
	ReasonLimit       = 200 // why one optional read or part was refused (parts.unread_reason)
	VerdictLimit      = 300 // a remote verdict quoted in an error
	SaidLimit         = 240 // the last line a child printed
	AnalyticsValueMax = 512 // an analytics dimension value; the contract's maxLength
	isoDateLength     = 10  // YYYY-MM-DD
)

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
