package runtime

import (
	"errors"
	"fmt"
	"testing"
)

func TestClassifyReadsTheContractFieldsThroughWrapping(t *testing.T) {
	credential := &ProviderError{Message: "token refused", Failure: FailureClassCredential, Reason: "expired"}
	cases := []struct {
		name    string
		err     error
		failure FailureClass
		refusal Refusal
		reason  string
	}{
		{"plain error", errors.New("boom"), FailureClassUnclassified, RefusalUnclassified, ""},
		{"credential", credential, FailureClassCredential, RefusalCredential, "expired"},
		{"wrapped twice", fmt.Errorf("list zones: %w", fmt.Errorf("page 2: %w", credential)), FailureClassCredential, RefusalCredential, "expired"},
		{"permission", HTTPRefusal(403), FailureClassPermission, RefusalPermission, ""},
		{"network is no refusal", &ProviderError{Message: "provider unreachable", Failure: FailureClassNetwork}, FailureClassNetwork, RefusalUnclassified, ""},
		{"address is no refusal", &ProviderError{Failure: FailureClassAddress}, FailureClassAddress, RefusalUnclassified, ""},
		{"other status", HTTPRefusal(500), FailureClassUnclassified, RefusalUnclassified, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			failure, refusal, reason := Classify(c.err)
			if failure != c.failure || refusal != c.refusal || reason != c.reason {
				t.Errorf("got %q %q %q", failure, refusal, reason)
			}
		})
	}
}

func TestProviderErrorSaysWhatFailedThenWhy(t *testing.T) {
	cause := errors.New("connection reset")
	cases := []struct {
		err  *ProviderError
		want string
	}{
		{&ProviderError{Message: "provider unreachable"}, "provider unreachable"},
		{&ProviderError{Err: cause}, "connection reset"},
		{&ProviderError{Message: "read policy", Err: cause}, "read policy: connection reset"},
	}
	for _, c := range cases {
		if got := c.err.Error(); got != c.want {
			t.Errorf("got %q, want %q", got, c.want)
		}
	}
	if !errors.Is(&ProviderError{Message: "read policy", Err: cause}, cause) {
		t.Error("the cause is reachable through Unwrap")
	}
}

// A report carries HQ's words apart from the cause's own text, whatever
// context wraps them; an error with no cause named apart is all reason.
func TestSaidKeepsTheCauseApartFromTheReason(t *testing.T) {
	cause := errors.New("json: cannot unmarshal string into Go value of type int")
	cases := []struct {
		err            error
		reason, detail string
	}{
		{errors.New("boom"), "boom", ""},
		{&ProviderError{Message: "provider unreachable"}, "provider unreachable", ""},
		{&ProviderError{Err: cause}, cause.Error(), ""},
		{&ProviderError{Message: "cloudflare returned invalid rows", Err: cause}, "cloudflare returned invalid rows", cause.Error()},
		{fmt.Errorf("zone example.com: %w", &ProviderError{Message: "read rows", Err: cause}), "zone example.com: read rows", cause.Error()},
		{&ProviderError{Message: "connection example", Err: ErrNoSuchConnection}, "connection example: not supplied to the controller", ""},
	}
	for _, c := range cases {
		if reason, detail := Said(c.err); reason != c.reason || detail != c.detail {
			t.Errorf("%v: got %q %q", c.err, reason, detail)
		}
	}
}

func TestHTTPRefusalKeepsTheStatusAndClass(t *testing.T) {
	for code, want := range map[int]FailureClass{401: FailureClassCredential, 403: FailureClassPermission, 404: FailureClassUnclassified} {
		err := HTTPRefusal(code)
		if err.HTTPStatus != code || err.Failure != want || err.Error() != fmt.Sprintf("the service answered %d", code) {
			t.Errorf("%d: %+v", code, err)
		}
	}
}
