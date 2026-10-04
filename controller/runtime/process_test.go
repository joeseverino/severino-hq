package runtime

import (
	"bytes"
	"io"
	"slices"
	"testing"
)

func TestBoundedBufferKeepsTheLimitAndNotesTheRest(t *testing.T) {
	buffer := BoundedBuffer{Limit: 4}
	if n, err := buffer.Write([]byte("abcdef")); n != 6 || err != nil {
		t.Fatalf("%d %v", n, err)
	}
	if buffer.String() != "abcd" || !buffer.Overflow {
		t.Fatalf("%q %v", buffer.String(), buffer.Overflow)
	}
}

// Regression: an embedded bytes.Buffer lent io.Copy its ReadFrom, which
// skipped Write and with it the limit on the bridge's output.
func TestBoundedBufferCapHoldsUnderIOCopy(t *testing.T) {
	buffer := BoundedBuffer{Limit: 1024}
	if _, ok := any(&buffer).(io.ReaderFrom); ok {
		t.Fatal("BoundedBuffer must not implement io.ReaderFrom")
	}
	if _, err := io.Copy(&buffer, bytes.NewReader(make([]byte, 1<<20))); err != nil {
		t.Fatal(err)
	}
	if len(buffer.Bytes()) != 1024 || !buffer.Overflow {
		t.Fatalf("kept %d bytes, overflow %v", len(buffer.Bytes()), buffer.Overflow)
	}
}

func TestChildEnvironmentCarriesNoCredential(t *testing.T) {
	env := Environment{"PATH": "/bin", "NPM_PASSWORD": "synthetic", "CLOUDFLARE_DNS_API_TOKEN": "synthetic"}
	got := env.ChildEnvironment(map[string]string{"OP_SERVICE_ACCOUNT_TOKEN": "t"})
	slices.Sort(got)
	if !slices.Equal(got, []string{"OP_SERVICE_ACCOUNT_TOKEN=t", "PATH=/bin"}) {
		t.Fatalf("%v", got)
	}
}

func TestWithoutConnectionsDropsEveryConnectionsValues(t *testing.T) {
	env := Environment{
		"DJANGO_SECRET_KEY": "k", "NPM_CONNECTION_REF": "proxy", "NPM_PASSWORD": "synthetic",
		"OP_SERVICE_ACCOUNT_TOKEN": "t", "HQ_MANAGE_PY": "/app/manage.py",
	}
	got := env.WithoutConnections()
	slices.Sort(got)
	if !slices.Equal(got, []string{"DJANGO_SECRET_KEY=k", "HQ_MANAGE_PY=/app/manage.py"}) {
		t.Fatalf("%v", got)
	}
}
