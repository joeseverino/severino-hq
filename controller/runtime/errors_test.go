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

func TestHTTPRefusalKeepsTheStatusAndClass(t *testing.T) {
	for code, want := range map[int]FailureClass{401: FailureClassCredential, 403: FailureClassPermission, 404: FailureClassUnclassified} {
		err := HTTPRefusal(code)
		if err.HTTPStatus != code || err.Failure != want || err.Error() != fmt.Sprintf("provider answered %d", code) {
			t.Errorf("%d: %+v", code, err)
		}
	}
}
