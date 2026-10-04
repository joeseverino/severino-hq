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

func (b CommandBridge) Call(ctx context.Context, args []string, payload any, result any) error {
	if len(b.Prefix) == 0 {
		return &BridgeError{"bridge command is not configured"}
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
		timeout = BridgeTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, b.Prefix[0], argv...)
	cmd.Stdin = bytes.NewReader(input)
	cmd.Env = b.Env
	cmd.WaitDelay = ProcessWaitDelay
	stdout, stderr := BoundedBuffer{Limit: MaxBridgeOutput}, BoundedBuffer{Limit: MaxBridgeOutput}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return &BridgeError{"bridge did not complete before its deadline"}
		}
		if _, ok := errors.AsType[*exec.ExitError](err); !ok {
			return &BridgeError{"bridge could not start"}
		}
		lines := strings.Split(strings.TrimSpace(stderr.String()), "\n")
		return &BridgeError{"bridge failed: " + Clip(lines[len(lines)-1], VerdictLimit)}
	}
	if stdout.Overflow {
		return &BridgeError{"bridge returned too much data"}
	}
	if result == nil {
		result = new(any)
	}
	// Strict: a field the contract does not declare is drift, refused here
	// rather than dropped.
	decoder := json.NewDecoder(bytes.NewReader(stdout.Bytes()))
	decoder.UseNumber()
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(result); err != nil {
		return &BridgeError{"bridge answer does not match the contract: " + err.Error()}
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return &BridgeError{"bridge returned trailing data"}
	}
	return nil
}
