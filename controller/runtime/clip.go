package runtime

import (
	"strings"
	"unicode/utf8"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/mask"
)

// Limits on text that crosses into a report, a log line or an error, cut by
// characters.
const (
	ReportTextLimit = 500 // a failure's text in a report
	ReasonLimit     = 200 // why one optional read or part was refused
	VerdictLimit    = 300 // a remote verdict quoted in an error
	SaidLimit       = 240 // one line of a child's or a job's output
	isoDateLength   = 10  // YYYY-MM-DD
)

// AnalyticsValueMax is the longest analytics dimension value the contract
// accepts.
var AnalyticsValueMax = api.MustLimit("AnalyticsRow", "properties", "value", "maxLength")

// Clip cuts s to at most n characters, with every learned secret struck
// first (mask.Text), so a cut never leaves part of one behind. It cuts between
// characters, never inside a multi-byte one.
func Clip(s string, n int) string {
	s = mask.Text(s)
	if n < 0 || len(s) <= n {
		return s
	}
	if runes := []rune(s); len(runes) > n {
		return string(runes[:n])
	}
	return s
}

// Line is one line of output as a report says it: clipped to SaidLimit,
// ending in an ellipsis when cut.
func Line(s string) string {
	if s = mask.Text(s); utf8.RuneCountInString(s) <= SaidLimit {
		return s
	}
	return Clip(s, SaidLimit-1) + "…"
}

// LastLine is the last line of a child's output that is not blank, trimmed
// and said as Line; empty when every line is blank.
func LastLine(output string) string {
	last := ""
	for line := range strings.SplitSeq(output, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			last = line
		}
	}
	return Line(last)
}

// ReportText is failure text as a report carries it.
func ReportText(s string) string { return Clip(s, ReportTextLimit) }

// ISODate is the date part of an ISO-8601 timestamp.
func ISODate(s string) string { return Clip(s, isoDateLength) }
