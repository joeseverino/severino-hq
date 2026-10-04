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
	// Env is the bridge process's environment; nil inherits this one.
	Env []string
}

type BridgeError struct{ Message string }

func (e *BridgeError) Error() string { return e.Message }

const maxBridgeOutput = 64 << 20

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
	cmd.Env = b.Env
	cmd.WaitDelay = ProcessWaitDelay
	stdout, stderr := BoundedBuffer{Limit: maxBridgeOutput}, BoundedBuffer{Limit: maxBridgeOutput}
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
	if stdout.Overflow {
		return &BridgeError{"HQ controller bridge returned too much data."}
	}
	if result == nil {
		result = new(any)
	}
	decoder := json.NewDecoder(bytes.NewReader(stdout.Bytes()))
	decoder.UseNumber()
	if err := decoder.Decode(result); err != nil {
		return &BridgeError{"HQ controller bridge returned invalid JSON."}
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return &BridgeError{"HQ controller bridge returned trailing data."}
	}
	return nil
}
