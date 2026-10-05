package runtime

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestClipCutsBetweenCharacters(t *testing.T) {
	for _, tc := range []struct {
		in   string
		n    int
		want string
	}{
		{"abc", 5, "abc"},
		{"abcdef", 3, "abc"},
		{"héllo", 2, "hé"},
		{"日本語です", 3, "日本語"},
		{"", 3, ""},
	} {
		if got := Clip(tc.in, tc.n); got != tc.want || !utf8.ValidString(got) {
			t.Errorf("Clip(%q, %d) = %q, want %q", tc.in, tc.n, got, tc.want)
		}
	}
	if got := ISODate("2027-01-01T00:00:00Z"); got != "2027-01-01" {
		t.Errorf("ISODate = %q", got)
	}
}

func TestActionRefusalTextIsCappedInTheReport(t *testing.T) {
	w, b, p, _ := newWorker()
	b.Pending = []Pending{pending("first")}
	p.ExecErr = &ProviderError{Message: strings.Repeat("é", ReportTextLimit+50)}
	if _, err := w.Run(t.Context(), true); err != nil {
		t.Fatal(err)
	}
	for _, call := range b.Calls {
		if call.Action != "report" {
			continue
		}
		report := call.Payload.(ControllerReport)
		if n := utf8.RuneCountInString(report.Message); n != ReportTextLimit {
			t.Fatalf("message is %d characters", n)
		}
		if n := utf8.RuneCountInString(report.Conditions[0].Message); n != ReportTextLimit {
			t.Fatalf("condition is %d characters", n)
		}
	}
}
