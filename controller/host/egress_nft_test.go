package host

import (
	"go/ast"
	"go/parser"
	"go/token"
	"maps"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// Every table hq-host can load is loaded by nft itself, in check mode, so a
// table the kernel or nft would refuse fails here rather than on a run.
// nft(8): "-c, --check: Check commands validity without actually applying the
// changes." It talks to the kernel's netlink, so it needs CAP_NET_ADMIN:
// root, a user and network namespace of its own, or sudo, which GitHub's
// Linux runners grant without a password ("Administrative privileges" in
// GitHub's hosted-runner reference).
func TestEveryEgressTableLoadsInNft(t *testing.T) {
	tables := confinements(t)
	nft := nftChecker(t)
	one := []netip.AddrPort{netip.MustParseAddrPort("192.0.2.10:22")}
	several := []netip.AddrPort{
		netip.MustParseAddrPort("192.0.2.10:22"),
		netip.MustParseAddrPort("192.0.2.10:7722"),
		netip.MustParseAddrPort("198.51.100.7:65535"),
	}
	for name, base := range tables {
		for _, reach := range [][]netip.AddrPort{nil, one, several} {
			confined := base
			confined.reach = reach
			if output, err := nft(confined.load()); err != nil {
				t.Errorf("nft refuses %s with %d destinations in reach: %v\n%s\n%s", name, len(reach), err, output, confined.load())
			}
		}
	}
}

// confinements is every confinement the package declares, by name, each a
// package variable whose value is a confinement literal; the source is read,
// so a confinement added later is checked without being listed here.
func confinements(t *testing.T) map[string]confinement {
	known := map[string]confinement{"renovateEgress": renovateEgress, "automationEgress": automationEgress}
	sources, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	declared := map[string]bool{}
	for _, source := range sources {
		if strings.HasSuffix(source, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(token.NewFileSet(), source, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		named := map[ast.Expr]bool{}
		ast.Inspect(file, func(node ast.Node) bool {
			if spec, ok := node.(*ast.ValueSpec); ok {
				for i, value := range spec.Values {
					if isConfinement(value) {
						declared[spec.Names[i].Name], named[value] = true, true
					}
				}
			}
			if value, ok := node.(ast.Expr); ok && isConfinement(value) && !named[value] {
				t.Errorf("%s builds a confinement outside a variable of its own, which this test cannot load", source)
			}
			return true
		})
	}
	if got := slices.Sorted(maps.Keys(declared)); !slices.Equal(got, slices.Sorted(maps.Keys(known))) {
		t.Fatalf("the package declares the confinements %v; name each in confinements", got)
	}
	return known
}

func isConfinement(value ast.Expr) bool {
	literal, ok := value.(*ast.CompositeLit)
	if !ok {
		return false
	}
	name, ok := literal.Type.(*ast.Ident)
	return ok && name.Name == "confinement"
}

// nftChecker is `nft -c -f` run with the first privilege that can load a
// known-good table, so a refusal it then gives is the table's. Without one
// the test skips, saying why, except in CI, which installs nft and has sudo.
func nftChecker(t *testing.T) func(table string) ([]byte, error) {
	if _, err := exec.LookPath("nft"); err != nil {
		unavailable(t, "nft is not installed")
	}
	check := func(prefix []string, table string) ([]byte, error) {
		path := filepath.Join(t.TempDir(), "table.nft")
		if err := os.WriteFile(path, []byte(table), 0o600); err != nil {
			t.Fatal(err)
		}
		command := append(slices.Clone(prefix), "nft", "-c", "-f", path)
		return exec.CommandContext(t.Context(), command[0], command[1:]...).CombinedOutput()
	}
	prefixes := [][]string{{"unshare", "-rn"}, {"sudo", "-n"}}
	if os.Geteuid() == 0 {
		prefixes = [][]string{{"env"}}
	}
	probe := "table inet severino_hq_probe {\n\tchain input {\n\t\ttype filter hook input priority filter; policy accept;\n\t}\n}\n"
	var refusals []string
	for _, prefix := range prefixes {
		output, err := check(prefix, probe)
		if err == nil {
			return func(table string) ([]byte, error) { return check(prefix, table) }
		}
		refusals = append(refusals, strings.Join(prefix, " ")+": "+strings.TrimSpace(string(output)))
	}
	unavailable(t, "no privilege loads a table in nft ("+strings.Join(refusals, "; ")+")")
	return nil
}

// unavailable skips with the reason, and fails in CI, where the check must run.
func unavailable(t *testing.T, reason string) {
	t.Helper()
	if os.Getenv("CI") == "true" {
		t.Fatal(reason)
	}
	t.Skip(reason)
}
