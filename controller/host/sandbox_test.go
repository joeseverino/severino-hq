package host

import (
	"fmt"
	"go/ast"
	"go/constant"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"maps"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"golang.org/x/tools/go/callgraph/rta"
	"golang.org/x/tools/go/packages"
	"golang.org/x/tools/go/ssa"
	"golang.org/x/tools/go/ssa/ssautil"
	"golang.org/x/tools/go/types/typeutil"
)

// hostRead is a path under /proc or /sys a program's own code reads, as far
// as its source states it: whole when every part of it is a constant, else
// the constant start of it.
type hostRead struct {
	path  string
	whole bool
	at    string
}

// apiTrees are the kernel's file systems a sandbox narrows.
var apiTrees = []string{"/proc", "/sys"}

func underAPI(p string) bool {
	return slices.ContainsFunc(apiTrees, func(tree string) bool { return p == tree || strings.HasPrefix(p, tree+"/") })
}

var (
	readsOnce sync.Once
	reads     map[string][]hostRead
	readsErr  error
)

// programReads is every path under /proc or /sys each program a shipped
// service runs, by name, may read from its own code (this module's, built for Linux) may read: each path stated in the
// source of a function the program can reach from main, by rapid type
// analysis, or of a package variable it links. A path is stated as a
// constant, a concatenation, a path.Join or filepath.Join, or a fmt.Sprintf
// format, each to its first part that is not constant.
func programReads(t *testing.T) map[string][]hostRead {
	t.Helper()
	readsOnce.Do(func() {
		var programs []string
		for _, program := range unitPrograms(t, shippedUnits) {
			if !slices.Contains(programs, program) {
				programs = append(programs, program)
			}
		}
		slices.Sort(programs)
		reads, readsErr = loadProgramReads(programs)
	})
	if readsErr != nil {
		t.Fatal(readsErr)
	}
	return reads
}

func loadProgramReads(programs []string) (map[string][]hostRead, error) {
	config := &packages.Config{
		Mode: packages.LoadAllSyntax | packages.NeedModule,
		Dir:  "..",
		Env:  append(os.Environ(), "GOOS=linux", "GOARCH=amd64", "CGO_ENABLED=0"),
	}
	var patterns []string
	for _, program := range programs {
		patterns = append(patterns, "./cmd/"+program)
	}
	loaded, err := packages.Load(config, patterns...)
	if err != nil {
		return nil, err
	}
	if packages.PrintErrors(loaded) > 0 {
		return nil, fmt.Errorf("the programs %v do not load", programs)
	}
	module := loaded[0].Module.Path
	byPath := map[string]*packages.Package{}
	packages.Visit(loaded, nil, func(p *packages.Package) { byPath[p.PkgPath] = p })
	prog, mains := ssautil.AllPackages(loaded, ssa.InstantiateGenerics)
	prog.Build()

	found := map[string][]hostRead{}
	for i, main := range mains {
		roots := []*ssa.Function{main.Func("main"), main.Func("init")}
		seen := map[token.Pos]bool{}
		record := func(info *types.Info, node ast.Node) {
			ast.Inspect(node, func(n ast.Node) bool {
				expr, ok := n.(ast.Expr)
				if !ok {
					return true
				}
				p, whole, ok := statedPath(info, expr)
				if !ok || !underAPI(p) {
					return true
				}
				if !seen[expr.Pos()] {
					seen[expr.Pos()] = true
					found[programs[i]] = append(found[programs[i]], hostRead{p, whole, prog.Fset.Position(expr.Pos()).String()})
				}
				return false
			})
		}
		for fn := range rta.Analyze(roots, false).Reachable {
			syntax := fn.Syntax()
			if syntax == nil || fn.Pkg == nil || !strings.HasPrefix(fn.Pkg.Pkg.Path(), module) {
				continue
			}
			record(byPath[fn.Pkg.Pkg.Path()].TypesInfo, syntax)
		}
		packages.Visit([]*packages.Package{loaded[i]}, nil, func(p *packages.Package) {
			if !strings.HasPrefix(p.PkgPath, module) {
				return
			}
			for _, file := range p.Syntax {
				for _, decl := range file.Decls {
					if gen, ok := decl.(*ast.GenDecl); ok && gen.Tok == token.VAR {
						record(p.TypesInfo, gen)
					}
				}
			}
		})
	}
	return found, nil
}

// statedPath is the path expr states, and whether all of it is constant.
func statedPath(info *types.Info, expr ast.Expr) (string, bool, bool) {
	if tv, ok := info.Types[expr]; ok && tv.Value != nil {
		if tv.Value.Kind() != constant.String {
			return "", false, false
		}
		return constant.StringVal(tv.Value), true, true
	}
	switch e := expr.(type) {
	case *ast.ParenExpr:
		return statedPath(info, e.X)
	case *ast.BinaryExpr:
		if start, _, ok := statedPath(info, e.X); ok && e.Op == token.ADD {
			return start, false, true
		}
	case *ast.CallExpr:
		callee, _ := typeutil.Callee(info, e).(*types.Func)
		if callee == nil || callee.Pkg() == nil {
			return "", false, false
		}
		switch callee.Pkg().Path() + "." + callee.Name() {
		case "path/filepath.Join", "path.Join":
			var parts []string
			for _, arg := range e.Args {
				part, whole, ok := statedPath(info, arg)
				if !ok || !whole || e.Ellipsis.IsValid() {
					return path.Join(parts...), false, len(parts) > 0
				}
				parts = append(parts, part)
			}
			return path.Join(parts...), true, len(parts) > 0
		case "fmt.Sprintf":
			if format, whole, ok := statedPath(info, e.Args[0]); ok && whole {
				start, _, verb := strings.Cut(format, "%")
				return start, !verb, true
			}
		}
	}
	return "", false, false
}

// sandbox is what a unit ends with, across its file and every drop-in that
// reaches it, of each setting that can hide a path from it.
type sandbox struct {
	user, procSubset, protectProc, tunables, kernelLogs string
	inaccessible, temporary, bound                      []string
}

func resolvedSandbox(t *testing.T, dir, unit string) sandbox {
	t.Helper()
	last := func(key string) string { v, _ := effective(t, dir, unit, key); return v }
	list := func(key string) []string { _, l := effective(t, dir, unit, key); return l }
	var bound []string
	for _, key := range []string{"BindPaths", "BindReadOnlyPaths"} {
		for _, entry := range list(key) {
			parts := strings.Split(strings.TrimLeft(entry, "-"), ":")
			bound = append(bound, parts[min(1, len(parts)-1)])
		}
	}
	return sandbox{
		user: last("User"), procSubset: last("ProcSubset"), protectProc: last("ProtectProc"),
		tunables: last("ProtectKernelTunables"), kernelLogs: last("ProtectKernelLogs"),
		inaccessible: list("InaccessiblePaths"), temporary: list("TemporaryFileSystem"), bound: bound,
	}
}

func within(p, dir string) bool {
	return p == dir || strings.HasPrefix(p, strings.TrimSuffix(dir, "/")+"/")
}

// hides is the setting of s that keeps a read of p from finding it, or "".
// Each rule is systemd.exec(5)'s, as systemd 259 (the deploy host's) states
// it, and the kernel's for procfs's mount options
// (docs.kernel.org/filesystems/proc.html, "Mount options"):
//   - InaccessiblePaths= makes each path and everything below it
//     inaccessible; a "-" or "+" before one changes only how it is found.
//   - TemporaryFileSystem= mounts an empty tmpfs on each mount point, which
//     hides what was there except what BindPaths= or BindReadOnlyPaths=
//     brings back.
//   - ProcSubset=pid "hides all top level files and directories in the procfs
//     that are not related to tasks": a process's directory, self and
//     thread-self stay.
//   - ProtectKernelTunables= makes /proc/sys, /sys and other kernel files
//     read-only, which hides none, and /proc/kallsyms and /proc/kcore
//     inaccessible.
//   - ProtectKernelLogs= makes /proc/kmsg inaccessible.
//   - ProtectProc= hides other users' process directories, and leaves root
//     unaffected.
//   - ProtectSystem= and ReadOnlyPaths= make paths read-only and hide none,
//     and strict leaves /proc and /sys as they are; ProtectControlGroups=
//     makes the cgroup tree read-only. None is read here, since a read is
//     all a host read does.
func (s sandbox) hides(p string) string {
	for _, entry := range s.inaccessible {
		if within(p, strings.TrimLeft(entry, "-+")) {
			return "InaccessiblePaths=" + entry
		}
	}
	for _, entry := range s.temporary {
		mount, _, _ := strings.Cut(entry, ":")
		if within(p, mount) && !slices.ContainsFunc(s.bound, func(dest string) bool { return within(p, dest) }) {
			return "TemporaryFileSystem=" + entry
		}
	}
	entry, isProc := strings.CutPrefix(p, "/proc/")
	if !isProc {
		return ""
	}
	entry, _, _ = strings.Cut(entry, "/")
	process := entry == "self" || entry == "thread-self" || strings.Trim(entry, "0123456789") == ""
	switch {
	case s.procSubset == "pid" && !process:
		return "ProcSubset=pid"
	case systemdTrue(s.tunables) && (entry == "kallsyms" || entry == "kcore"):
		return "ProtectKernelTunables=" + s.tunables
	case systemdTrue(s.kernelLogs) && entry == "kmsg":
		return "ProtectKernelLogs=" + s.kernelLogs
	case process && entry != "self" && entry != "thread-self" && s.protectProc != "" && s.protectProc != "default" &&
		s.user != "" && s.user != "root" && s.user != "0":
		return "ProtectProc=" + s.protectProc
	}
	return ""
}

// unitPrograms is each shipped service in dir that runs a program of the
// tree, by service, with that program's name.
func unitPrograms(t *testing.T, dir string) map[string]string {
	t.Helper()
	shipped, err := Shipped(dir)
	if err != nil {
		t.Fatal(err)
	}
	runs := map[string]string{}
	for _, unit := range shipped {
		if !strings.Contains(unit, "/") && strings.HasSuffix(unit, ".service") {
			if program := unitProgram(unitValue(t, filepath.Join(dir, unit), "ExecStart")); program != "" {
				runs[unit] = program
			}
		}
	}
	return runs
}

// hiddenReadBreaks is every path under /proc or /sys a program reads that
// the sandbox of a shipped service running it hides, and every such path its
// source builds at run time, where nothing can say whether it is hidden.
func hiddenReadBreaks(t *testing.T, dir string) []string {
	t.Helper()
	runs := unitPrograms(t, dir)
	found := programReads(t)
	var breaks []string
	for _, unit := range slices.Sorted(maps.Keys(runs)) {
		program := runs[unit]
		if _, loaded := found[program]; !loaded {
			breaks = append(breaks, fmt.Sprintf("%s runs %s, which no shipped service ran when its reads were walked", unit, program))
			continue
		}
		resolved := resolvedSandbox(t, dir, unit)
		for _, read := range found[program] {
			if !read.whole && slices.Contains([]string{"/proc", "/proc/", "/sys", "/sys/"}, read.path) {
				breaks = append(breaks, fmt.Sprintf("%s: %s reads a path under %s built at run time, which no sandbox can be checked against: state it as a constant", read.at, program, read.path))
				continue
			}
			if setting := resolved.hides(read.path); setting != "" {
				breaks = append(breaks, fmt.Sprintf("%s runs %s, which reads %s (%s), but its sandbox hides it: %s", unit, program, read.path, read.at, setting))
			}
		}
	}
	return breaks
}

func TestEveryHostReadIsVisibleInTheSandboxOfEachUnitThatMakesIt(t *testing.T) {
	for _, broken := range hiddenReadBreaks(t, shippedUnits) {
		t.Error(broken)
	}
	// The walk finds the renderer's read of the port floor, so it reads the
	// code that reads the host.
	found := false
	for _, read := range programReads(t)["hq-secrets"] {
		found = found || read.path == "/proc/sys/net/ipv4/ip_unprivileged_port_start"
	}
	if !found {
		t.Error("the walk does not find hq-secrets' read of net.ipv4.ip_unprivileged_port_start")
	}
}

// The shared sandbox hides /proc/sys, so the renderer's unit without its own
// ProcSubset=all hides the port floor it reads.
func TestASandboxThatHidesAHostReadIsNamed(t *testing.T) {
	dir := copyUnits(t)
	conf := filepath.Join(dir, "severino-hq-secrets.service.d", "30-sandbox.conf")
	data, err := os.ReadFile(conf)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(conf, []byte(strings.ReplaceAll(string(data), "ProcSubset=all", "ProcSubset=pid")), 0o644); err != nil {
		t.Fatal(err)
	}
	breaks := hiddenReadBreaks(t, dir)
	if len(breaks) != 1 || !strings.HasPrefix(breaks[0], "severino-hq-secrets.service runs hq-secrets, which reads /proc/sys/net/ipv4/ip_unprivileged_port_start") {
		t.Errorf("named %v, want the renderer's port floor alone", breaks)
	}
}

func TestEachSettingThatHidesAPathIsRead(t *testing.T) {
	for _, c := range []struct {
		name, body, path, hiddenBy string
	}{
		{"the subset hides a kernel file", "ProcSubset=pid\n", "/proc/sys/kernel/random/uuid", "ProcSubset=pid"},
		{"the subset keeps the process's own", "ProcSubset=pid\n", "/proc/self/mountinfo", ""},
		{"a later subset replaces an earlier", "ProcSubset=pid\nProcSubset=all\n", "/proc/meminfo", ""},
		{"an inaccessible path hides what is below it", "InaccessiblePaths=-/proc/self\n", "/proc/self/mounts", "InaccessiblePaths=-/proc/self"},
		{"an emptied list hides nothing", "InaccessiblePaths=/sys\nInaccessiblePaths=\n", "/sys/fs/cgroup", ""},
		{"a tmpfs hides what it covers", "TemporaryFileSystem=/sys:ro\n", "/sys/class/net", "TemporaryFileSystem=/sys:ro"},
		{"a bind brings a path back", "TemporaryFileSystem=/sys:ro\nBindReadOnlyPaths=/sys/class\n", "/sys/class/net", ""},
		{"tunables hide the kernel's symbols", "ProtectKernelTunables=yes\n", "/proc/kallsyms", "ProtectKernelTunables=yes"},
		{"tunables only make /proc/sys read-only", "ProtectKernelTunables=yes\n", "/proc/sys/net/ipv4/ip_unprivileged_port_start", ""},
		{"kernel logs hide the ring buffer", "ProtectKernelLogs=yes\n", "/proc/kmsg", "ProtectKernelLogs=yes"},
		{"root sees every process", "ProtectProc=invisible\n", "/proc/1/status", ""},
		{"another account does not", "ProtectProc=invisible\nUser=example\n", "/proc/1/status", "ProtectProc=invisible"},
		{"a read-only system hides nothing", "ProtectSystem=strict\nReadOnlyPaths=/proc\n", "/proc/meminfo", ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			if err := os.WriteFile(filepath.Join(dir, "severino-hq-example.service"), []byte("[Service]\n"+c.body), 0o644); err != nil {
				t.Fatal(err)
			}
			if got := resolvedSandbox(t, dir, "severino-hq-example.service").hides(c.path); got != c.hiddenBy {
				t.Errorf("%s is hidden by %q, want %q", c.path, got, c.hiddenBy)
			}
		})
	}
}

// A path is read in every form the walk names, to its first part that is
// not constant, and a path that only begins like one is not a read.
func TestEachFormAPathIsBuiltInIsRead(t *testing.T) {
	const source = `package example

import (
	"fmt"
	"path"
	"path/filepath"
)

const tree = "/proc"

func reads(pid int, name string) []string {
	return []string{
		tree + "/sys/kernel/osrelease",
		filepath.Join("/", "sys", "class", "net"),
		path.Join(tree, "self", name),
		fmt.Sprintf("/proc/%d/status", pid),
		"/proc/" + name,
		"/system/df",
	}
}
`
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "example.go", source, 0)
	if err != nil {
		t.Fatal(err)
	}
	info := &types.Info{Types: map[ast.Expr]types.TypeAndValue{}, Uses: map[*ast.Ident]types.Object{}, Selections: map[*ast.SelectorExpr]*types.Selection{}}
	if _, err := (&types.Config{Importer: importer.Default()}).Check("example", fset, []*ast.File{file}, info); err != nil {
		t.Fatal(err)
	}
	var got []hostRead
	ast.Inspect(file.Decls[len(file.Decls)-1], func(n ast.Node) bool {
		if expr, ok := n.(ast.Expr); ok {
			if p, whole, ok := statedPath(info, expr); ok && underAPI(p) {
				got = append(got, hostRead{path: p, whole: whole})
				return false
			}
		}
		return true
	})
	want := []hostRead{
		{path: "/proc/sys/kernel/osrelease", whole: true},
		{path: "/sys/class/net", whole: true},
		{path: "/proc/self", whole: false},
		{path: "/proc/", whole: false},
		{path: "/proc/", whole: false},
	}
	if !slices.Equal(got, want) {
		t.Errorf("read %v, want %v", got, want)
	}
}
