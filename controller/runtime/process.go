package runtime

import (
	"bytes"
	"time"
)

// ProcessWaitDelay bounds how long a killed child's pipes are waited on, so a
// grandchild holding them open cannot outlive the deadline.
const ProcessWaitDelay = 5 * time.Second

// BoundedBuffer keeps at most Limit bytes of a child's output and notes when
// more arrived. Writes never fail, so the child is not killed by a short write.
// The buffer is a field, not embedded: an embedded bytes.Buffer would lend
// io.Copy its ReadFrom and bypass the limit.
type BoundedBuffer struct {
	buffer   bytes.Buffer
	Limit    int
	Overflow bool
}

func (b *BoundedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	remaining := max(b.Limit-b.buffer.Len(), 0)
	if len(p) > remaining {
		p = p[:remaining]
		b.Overflow = true
	}
	_, _ = b.buffer.Write(p)
	return n, nil
}

func (b *BoundedBuffer) Bytes() []byte  { return b.buffer.Bytes() }
func (b *BoundedBuffer) String() string { return b.buffer.String() }

// childEnvironment names the variables a child process may inherit: what
// locates tools, locale and temp space, and TLS roots. Credentials are passed
// per call, never inherited.
var childEnvironment = []string{
	"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
	"SSL_CERT_FILE", "SSL_CERT_DIR",
}

// ChildEnvironment is the allowlisted part of the environment plus overrides,
// which win.
func (e Environment) ChildEnvironment(overrides map[string]string) []string {
	env := []string{}
	for _, name := range childEnvironment {
		if _, replaced := overrides[name]; replaced {
			continue
		}
		if value, ok := e[name]; ok {
			env = append(env, name+"="+value)
		}
	}
	for name, value := range overrides {
		env = append(env, name+"="+value)
	}
	return env
}
