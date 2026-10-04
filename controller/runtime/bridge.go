package runtime

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"time"
)

// CommandBridge keeps Django as the transaction and persistence owner. Prefix
// is a trusted argv vector, e.g. python /app/manage.py infrastructure_controller.
type CommandBridge struct {
	Prefix  []string
	Timeout time.Duration
}

type BridgeError struct{ Message string }

func (e *BridgeError) Error() string { return e.Message }

const maxBridgeOutput = 64 << 20

type boundedBuffer struct {
	bytes.Buffer
	overflow bool
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	remaining := maxBridgeOutput - b.Len()
	if len(p) > remaining {
		p = p[:remaining]
		b.overflow = true
	}
	_, _ = b.Buffer.Write(p)
	return n, nil
}

func (b CommandBridge) Call(ctx context.Context, args []string, payload any, result any) error {
	if len(b.Prefix) == 0 {
		return &BridgeError{"Controller bridge command is not configured."}
	}
	argv := append(append([]string{}, b.Prefix[1:]...), args...)
	var input []byte
	if payload != nil {
		var err error
		input, err = json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("encode bridge payload: %w", err)
		}
		argv = append(argv, "--payload", "-")
	}
	timeout := b.Timeout
	if timeout == 0 {
		timeout = 3 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, b.Prefix[0], argv...)
	cmd.Stdin = bytes.NewReader(input)
	var stdout, stderr boundedBuffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return &BridgeError{"HQ controller bridge did not complete before its deadline."}
		}
		var exited *exec.ExitError
		if !errors.As(err, &exited) {
			return &BridgeError{"HQ controller bridge could not start."}
		}
		lines := strings.Split(strings.TrimSpace(stderr.String()), "\n")
		said := []rune(lines[len(lines)-1])
		if len(said) > 300 {
			said = said[:300]
		}
		return &BridgeError{"HQ controller bridge command failed: " + string(said)}
	}
	if stdout.overflow {
		return &BridgeError{"HQ controller bridge returned too much data."}
	}
	if result == nil {
		result = new(any)
	}
	decoder := json.NewDecoder(&stdout.Buffer)
	decoder.UseNumber()
	if err := decoder.Decode(result); err != nil {
		return &BridgeError{"HQ controller bridge returned invalid JSON."}
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return &BridgeError{"HQ controller bridge returned trailing data."}
	}
	return nil
}
