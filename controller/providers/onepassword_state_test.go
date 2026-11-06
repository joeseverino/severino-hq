package providers

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// op is told where to keep its state: a directory of the run's own, private
// to the account, and never the image's tree.
func TestOpIsGivenAPrivateDirectoryForItsState(t *testing.T) {
	env := opEnvironment("example-token")
	if env["OP_SERVICE_ACCOUNT_TOKEN"] != "example-token" {
		t.Fatalf("the token did not reach op: %v", env)
	}
	dir := env["OP_CONFIG_DIR"]
	info, err := os.Stat(dir)
	if err != nil || !info.IsDir() {
		t.Fatalf("OP_CONFIG_DIR %q is not a directory: %v", dir, err)
	}
	if info.Mode().Perm() != 0o700 {
		t.Fatalf("OP_CONFIG_DIR is %o, want 0700", info.Mode().Perm())
	}
	if again := opEnvironment("other")["OP_CONFIG_DIR"]; again != dir {
		t.Fatalf("a second call made another directory: %q then %q", dir, again)
	}
	if len(env) != 2 {
		t.Fatalf("op is given more than its token and its directory: %v", env)
	}
	// It lives as long as the process, so a test run's goes with the run's
	// own temporary directory (TestMain) rather than staying in the shared one.
	if run := filepath.Base(os.TempDir()); !strings.HasPrefix(run, "hq-providers-test-") || filepath.Dir(dir) != os.TempDir() {
		t.Fatalf("OP_CONFIG_DIR %q is outside the run's own temporary directory", dir)
	}
}
