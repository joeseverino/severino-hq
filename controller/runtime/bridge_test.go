package runtime

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"
)

// The test binary acts as the bridge child; no shell, Python or live HQ needed.
func TestBridgeChild(t *testing.T) {
	if os.Getenv("HQ_TEST_BRIDGE_CHILD") != "1" {
		return
	}
	mode := os.Args[len(os.Args)-1]
	switch mode {
	case "bad":
		fmt.Print("not json")
	case "trailing":
		fmt.Print("{} {}")
	case "fail":
		fmt.Fprintln(os.Stderr, "Traceback\nCommandError: Unknown kind.")
		os.Exit(1)
	case "wait":
		time.Sleep(time.Minute)
	case "drift":
		fmt.Print(`{"ok": true, "undeclared": 1}`)
	case "flood":
		os.Stdout.Write(make([]byte, MaxBridgeOutput+1))
	default:
		data, _ := io.ReadAll(os.Stdin)
		_ = json.NewEncoder(os.Stdout).Encode(Object{"args": os.Args, "bytes": len(data)})
	}
	os.Exit(0)
}
func childBridge(t *testing.T) CommandBridge {
	t.Helper()
	t.Setenv("HQ_TEST_BRIDGE_CHILD", "1")
	return CommandBridge{Prefix: []string{os.Args[0], "-test.run=^TestBridgeChild$", "--"}, Timeout: 5 * time.Second}
}
func TestLargePayloadUsesStdin(t *testing.T) {
	b := childBridge(t)
	var result struct {
		Args  []string
		Bytes int
	}
	err := b.Call(t.Context(), []string{"inventory"}, Object{"data": strings.Repeat("x", 300000)}, &result)
	if err != nil || result.Bytes < 300000 {
		t.Fatalf("%v %#v", err, result)
	}
	for _, arg := range result.Args {
		if len(arg) > 4096 {
			t.Fatal("payload in argv")
		}
	}
	if result.Args[len(result.Args)-1] != "-" {
		t.Fatal(result.Args)
	}
}
func TestBridgeRefusesInvalidAndTrailingJSON(t *testing.T) {
	for _, mode := range []string{"bad", "trailing"} {
		t.Run(mode, func(t *testing.T) {
			b := childBridge(t)
			if err := b.Call(t.Context(), []string{mode}, nil, new(Object)); err == nil {
				t.Fatal("accepted invalid output")
			}
		})
	}
}
func TestBridgeExplainsFailureAndBoundsExecution(t *testing.T) {
	b := childBridge(t)
	if err := b.Call(t.Context(), []string{"fail"}, nil, nil); err == nil || !strings.Contains(err.Error(), "Unknown kind") {
		t.Fatal(err)
	}
	b.Timeout = 20 * time.Millisecond
	if err := b.Call(t.Context(), []string{"wait"}, nil, nil); err == nil || !strings.Contains(err.Error(), "deadline") {
		t.Fatal(err)
	}
}

func TestBridgeOutputIsBounded(t *testing.T) {
	b := childBridge(t)
	if err := b.Call(t.Context(), []string{"flood"}, nil, nil); err == nil || !strings.Contains(err.Error(), "too much data") {
		t.Fatal(err)
	}
}

// A field the contract does not declare is refused, not silently dropped.
func TestBridgeRefusesFieldsTheContractDoesNotDeclare(t *testing.T) {
	b := childBridge(t)
	var into IdlePassOutput
	if err := b.Call(t.Context(), []string{"drift"}, nil, &into); err == nil || !strings.Contains(err.Error(), "undeclared") {
		t.Fatalf("%v", err)
	}
}
