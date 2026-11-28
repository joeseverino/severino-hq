package host

import (
	"context"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// The rehearsal's container and volumes, written out so these tests read the
// names a host sees.
const (
	rehearsed       = "severino-hq-rehearsal"
	rehearsedVolume = "severino-hq-rehearsal-data"
	planCall        = "docker run --rm --pull never --network host"
	walkCall        = "docker exec " + rehearsed + " timeout 180 python manage.py page_costs"
	// walked is a walk in which every page answered.
	walked = `{"revision": "", "pages": {"/": {"status": 200}, "/example-moved/": {"status": 302}}}`
)

// rehearsal is how the host answers a rehearsal's checks. Its zero value is a
// release that passes every one.
type rehearsal struct {
	notReady, noSocket, damaged, runsAnyJob, killed, liveRestarts bool
	planExit                                                      int
	marks                                                         int
	// pages is what the walk prints, walked when empty.
	pages string
}

// rehearses answers the rehearsal's checks as r says, over what the fake host
// already answers; a call f.fail names still fails.
func (f *fakeHost) rehearses(r *rehearsal) {
	program := f.machine.Command
	f.machine.Command = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		inner := program(ctx, name, args...)
		call := strings.Join(append([]string{name}, args...), " ")
		for _, prefix := range f.fail {
			if strings.HasPrefix(call, prefix) {
				return inner
			}
		}
		answer := func(out string, failed bool) *exec.Cmd {
			code := 0
			if failed {
				code = 1
			}
			return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
		}
		switch {
		case call == "docker exec "+rehearsed+" hq-controller ready http://127.0.0.1:8000/health/ready/":
			return answer("", r.notReady)
		case call == "docker inspect --format {{.State.Running}} "+rehearsed:
			return answer(strconv.FormatBool(!r.notReady)+"\n", false)
		case strings.HasPrefix(call, "docker exec "+rehearsed+" python") && strings.Contains(call, "S_ISSOCK"):
			return answer("", r.noSocket)
		case strings.HasPrefix(call, "docker exec "+rehearsed+" python") && strings.Contains(call, "integrity_check"):
			if r.damaged {
				return answer("*** in database main ***\nPage 7 is never used\n", false)
			}
			return answer("ok\n", false)
		case call == "docker exec "+rehearsed+" hq-controller job no.such.job":
			return answer("", !r.runsAnyJob)
		case call == "docker inspect --format {{.State.OOMKilled}} "+rehearsed:
			return answer(strconv.FormatBool(r.killed)+"\n", false)
		case call == walkCall:
			pages := r.pages
			if pages == "" {
				pages = walked
			}
			return exec.CommandContext(ctx, "/bin/sh", "-c", `echo "Booting" >&2; printf 'Booting\n%s\n' "$1"`, "sh", pages)
		case strings.HasPrefix(call, planCall):
			return exec.CommandContext(ctx, "/bin/sh", "-c", `echo '{"mode":"plan"}'; exit "$1"`, "sh", strconv.Itoa(r.planExit))
		case call == "docker inspect --format {{.State.StartedAt}} "+hostcore.Container:
			r.marks++
			if r.liveRestarts && r.marks > 1 {
				return answer("2026-10-18T11:00:00Z\n", false)
			}
			return answer("2026-10-18T09:00:00Z\n", false)
		}
		return inner
	}
}

// What a rehearsal left: every docker run comes before the last removal of
// its volumes, and its scratch directory is gone.
func (f *fakeHost) rehearsalLeftNothing(t *testing.T) {
	t.Helper()
	removed, lastRun := -1, -1
	for i, call := range f.calls() {
		if strings.HasPrefix(call, "docker volume rm --force "+rehearsedVolume) {
			removed = i
		}
		if strings.HasPrefix(call, "docker run") && !isSetup(call) {
			lastRun = i
		}
	}
	if removed < 0 || removed < lastRun {
		t.Errorf("the rehearsal's volumes outlived it:\n%s", strings.Join(f.calls(), "\n"))
	}
	if _, err := os.Stat(filepath.Join(f.machine.Runtime, rehearsed)); err == nil {
		t.Error("the rehearsal's scratch directory, with its copies of secrets, was left")
	}
}

// A release that fails any check of its rehearsal is refused as not deployed,
// and nothing live is held, synced, recorded, restarted or rendered.
func TestAFailedRehearsalLeavesProductionAsItWas(t *testing.T) {
	for _, tc := range []struct {
		name, says string
		host       rehearsal
		fail       string
	}{
		{name: "the web process never answers ready", says: "ready=no", host: rehearsal{notReady: true}},
		{name: "the database could not be copied", says: "ready=no", fail: "docker run --rm --pull never --user 10001:10001"},
		{name: "no bridge socket", says: "socket=missing", host: rehearsal{noSocket: true}},
		{name: "a damaged copy", says: "integrity=damaged", host: rehearsal{damaged: true}},
		{name: "a scheduled job fails", says: "jobs=failed", fail: "docker exec " + rehearsed + " hq-controller job audit.prune"},
		{name: "an undeclared job runs", says: "jobs=failed", host: rehearsal{runsAnyJob: true}},
		{name: "killed for memory", says: "memory=killed", host: rehearsal{killed: true}},
		{name: "the plan fails", says: "plan=exit-3", host: rehearsal{planExit: 3}},
		{name: "the live web restarted meanwhile", says: "live=restarted", host: rehearsal{liveRestarts: true}},
		{name: "the host's setup does not check", says: "setup=failed", fail: "docker run --rm --pull never --name severino-hq-setup-"},
		{name: "a page breaks on production's data", says: "pages=1-failed", host: rehearsal{
			pages: `{"revision": "", "pages": {"/": {"status": 200}, "/example-broken/": {"status": 500}}}`}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFakeHost(t)
			f.active = []string{"severino-hq-secrets.timer", "severino-hq-controller.path"}
			if err := f.machine.Record(running); err != nil {
				t.Fatal(err)
			}
			d, _ := newDeployer(f)
			host := tc.host
			f.rehearses(&host)
			if tc.fail != "" {
				f.fail = []string{tc.fail}
			}

			err := d.Release(context.Background(), next)

			if err == nil {
				t.Fatalf("a release that failed its rehearsal deployed:\n%s", f.out.String())
			}
			if got := OutcomeOf(err); got != DeployNotDeployed {
				t.Errorf("a refused release ended %s: %v", got.Name, err)
			}
			verdict := ""
			for line := range strings.SplitSeq(f.out.String(), "\n") {
				if strings.Contains(line, "result=") {
					verdict = line
				}
			}
			if !strings.Contains(verdict, "result=fail") || !strings.Contains(verdict, tc.says) {
				t.Errorf("the deploy's log does not say %s:\n%s", tc.says, f.out.String())
			}
			for _, call := range f.calls() {
				if strings.HasPrefix(call, "systemctl stop") || strings.HasPrefix(call, "systemctl start") ||
					strings.HasPrefix(call, "systemctl restart") || strings.HasPrefix(call, "systemctl daemon-reload") ||
					strings.Contains(call, SecretsUnit) {
					t.Errorf("a refused release touched the host: %s", call)
				}
			}
			if data, _ := os.ReadFile(f.machine.ReleasePath()); strings.TrimSpace(string(data)) != running {
				t.Errorf("a refused release was recorded: %q", data)
			}
			if _, err := os.Stat(filepath.Join(f.machine.Lib, "hq", "config", "synced-release")); err == nil {
				t.Error("a refused release's tree was synced")
			}
			if tc.host.notReady && f.called(planCall) {
				t.Error("the controller planned against a web process that never became ready")
			}
			f.rehearsalLeftNothing(t)
		})
	}
}

// A release that passes its rehearsal goes on to the hold and the rest of the
// release, and the rehearsal ran the verified image alone, the web process
// with no network, and the controller in plan mode on copies.
func TestAPassingRehearsalGoesOnToTheRelease(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{"severino-hq-secrets.timer"}
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	d, _ := newDeployer(f)
	f.rehearses(&rehearsal{})

	if err := d.Release(context.Background(), next); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	if !strings.Contains(f.out.String(), "result=pass") || !strings.Contains(f.out.String(), " setup=ok ") ||
		!strings.Contains(f.out.String(), " pages=ok ") {
		t.Errorf("no passing verdict in the deploy's log:\n%s", f.out.String())
	}
	if strings.Contains(f.out.String(), "/example-moved/") {
		t.Errorf("a page's name reached the deploy's log:\n%s", f.out.String())
	}
	kept, err := os.Stat(f.machine.RehearsalPagesPath())
	if err != nil || kept.Mode().Perm() != 0o600 {
		t.Fatalf("the walk was not kept for root alone: %v %v", kept, err)
	}
	if data, _ := os.ReadFile(f.machine.RehearsalPagesPath()); strings.TrimSpace(string(data)) != walked {
		t.Errorf("the kept walk is not the one printed: %q", data)
	}
	f.before(t, planCall, "systemctl stop severino-hq-secrets.timer")
	f.before(t, "docker volume rm --force "+rehearsedVolume, "systemctl stop severino-hq-secrets.timer")
	f.before(t, "systemctl stop severino-hq-secrets.timer", "systemctl restart "+hostcore.WebUnit)
	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != next {
		t.Errorf("the release is %q", data)
	}

	var web, plan string
	copied := false
	for _, call := range f.calls() {
		if !strings.HasPrefix(call, "docker run") || isSetup(call) {
			continue
		}
		if !strings.Contains(call, "--pull never") || !strings.Contains(call, " "+next) {
			t.Errorf("the rehearsal ran something other than the verified image: %s", call)
		}
		switch {
		case strings.HasPrefix(call, "docker run -d"):
			web = call
		case strings.HasPrefix(call, planCall):
			plan = call
		case strings.Contains(call, "source=severino-hq_severino_data,target=/src"):
			copied = strings.Contains(call, Database+"?mode=ro")
		}
	}
	if !copied {
		t.Error("production's database was not copied, read-only")
	}
	for _, flag := range []string{"--network none", "--cap-drop ALL", "no-new-privileges:true"} {
		if !strings.Contains(web, flag) {
			t.Errorf("the rehearsal's web process ran without %s: %s", flag, web)
		}
	}
	if strings.Contains(web, "severino-hq_severino_data") {
		t.Error("the rehearsal's web process was given the live database")
	}
	if strings.Contains(plan, "target=/data") || strings.Contains(plan, "--apply") {
		t.Errorf("the controller was given the database or applied: %s", plan)
	}
	if !strings.Contains(plan, filepath.Join(f.machine.Runtime, rehearsed, "plan", "connections.json")+",target=/run/secrets/controller-connections.json,readonly") {
		t.Errorf("the controller did not plan on a copy of the connections: %s", plan)
	}
	f.rehearsalLeftNothing(t)
}

// A disk without room for the copy refuses the release before anything is
// made.
func TestARehearsalNeedsRoomForItsCopy(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	database, err := os.Create(filepath.Join(f.data(), Database))
	if err != nil {
		t.Fatal(err)
	}
	if err := database.Truncate(64 << 20); err != nil {
		t.Fatal(err)
	}
	database.Close()
	d, _ := newDeployer(f)
	f.rehearses(&rehearsal{})
	d.Free = func(string) (uint64, error) { return d.FreeBytes + 100<<20, nil }

	if err := d.Release(context.Background(), next); err == nil {
		t.Fatal("a release deployed with no room to rehearse it")
	}
	if f.called("docker volume create") || f.called("systemctl stop") {
		t.Error("a release with no room to rehearse went on")
	}
}

// imageRunsFromTheTree gives the image being deployed a service that runs a
// program of its tree under the shared sandbox and a widening of its own.
func (f *fakeHost) imageRunsFromTheTree(t *testing.T) {
	t.Helper()
	for name, body := range map[string]string{
		"severino-hq-example.service":                   "[Unit]\nDescription=Example\n\n[Service]\nType=oneshot\nExecStartPre=/bin/rm -f /run/example\nExecStart=" + Lib + "/deploy/bin/hq-secrets\nTimeoutStartSec=4min\nRuntimeDirectory=example\nReadWritePaths=/run/example\n",
		"severino-hq-.service.d/20-sandbox.conf":        "[Service]\nProcSubset=pid\nCapabilityBoundingSet=\nSystemCallFilter=~@privileged @resources\n",
		"severino-hq-example.service.d/30-sandbox.conf": "[Service]\nProcSubset=all\nCapabilityBoundingSet=CAP_CHOWN\n",
		"severino-hq-other.service":                     "[Service]\nExecStart=/usr/bin/docker exec example true\n",
		"../bin/hq-secrets":                             "#!/bin/sh\n",
	} {
		path := filepath.Join(f.image, "deploy", "systemd", name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// Each program a shipped service runs from the image's tree starts once
// before anything live is held, in a transient unit no live drop-in reaches,
// under the sandbox the image's own files give that service, in the order
// systemd reads them, and with nothing of what the service runs or owns.
func TestARehearsalStartsEachProgramInItsUnitsSandbox(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{"severino-hq-secrets.timer"}
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	f.imageRunsFromTheTree(t)
	d, _ := newDeployer(f)
	f.rehearses(&rehearsal{})

	if err := d.Release(context.Background(), next); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	var runs []string
	for _, call := range f.calls() {
		if strings.HasPrefix(call, "systemd-run ") {
			runs = append(runs, call)
		}
	}
	if len(runs) != 1 {
		t.Fatalf("want one dry run, of the one service that runs a program of the tree:\n%s", strings.Join(runs, "\n"))
	}
	run := runs[0]
	f.before(t, "systemd-run ", "systemctl stop severino-hq-secrets.timer")
	if !strings.Contains(run, "--wait") || !strings.Contains(run, "--unit=hq-rehearsal-example.service") {
		t.Errorf("the dry run is not waited on as a unit of its own: %s", run)
	}
	program := filepath.Join(f.machine.Runtime, rehearsed, "tree", "deploy", "bin", "hq-secrets")
	if !strings.HasSuffix(run, " -- "+program+" dry-run") {
		t.Errorf("the dry run is not the image's program in its dry run: %s", run)
	}
	order := []string{"--property=ReadWritePaths=/run/example", "--property=ProcSubset=pid", "--property=CapabilityBoundingSet=",
		"--property=SystemCallFilter=~@privileged @resources", "--property=ProcSubset=all", "--property=CapabilityBoundingSet=CAP_CHOWN"}
	at := 0
	for _, property := range order {
		i := strings.Index(run[at:], property)
		if i < 0 {
			t.Fatalf("the dry run's sandbox lacks %s, or not after what systemd reads before it: %s", property, run)
		}
		at += i + len(property)
	}
	for _, left := range []string{"ExecStart", "Type=", "TimeoutStartSec", "RuntimeDirectory", "Description"} {
		if strings.Contains(run, "--property="+left) {
			t.Errorf("the dry run was given %s: %s", left, run)
		}
	}
	f.rehearsalLeftNothing(t)
}

// A program its sandbox refuses fails the rehearsal, naming the service, and
// nothing live is touched.
func TestARefusedDryRunFailsTheRehearsal(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{"severino-hq-secrets.timer"}
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	f.imageRunsFromTheTree(t)
	d, _ := newDeployer(f)
	f.rehearses(&rehearsal{})
	f.fail = []string{"systemd-run "}

	err := d.Release(context.Background(), next)

	if err == nil || OutcomeOf(err) != DeployNotDeployed {
		t.Fatalf("a release its sandbox refused ended %v:\n%s", err, f.out.String())
	}
	if !strings.Contains(f.out.String(), "result=fail sandbox=refused-severino-hq-example ") {
		t.Errorf("the verdict does not name the refused service:\n%s", f.out.String())
	}
	if f.called("systemctl stop") {
		t.Error("a release its sandbox refused held the live units")
	}
	f.rehearsalLeftNothing(t)
}

// A walk that cannot run, or reads no page, says nothing of the release: the
// release goes on, and no walk of an earlier release is left in its place.
func TestAnUnreadWalkDeploysTheRelease(t *testing.T) {
	for _, tc := range []struct {
		name  string
		pages string
		fail  string
	}{
		{name: "the walk is cut off", fail: walkCall},
		{name: "no operator to sign in as", pages: `{"revision": "", "pages": {}, "unread": "no active operator to sign in as"}`},
		{name: "a document it cannot read", pages: "Traceback"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFakeHost(t)
			if err := f.machine.Record(running); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(f.machine.RehearsalPagesPath(), []byte(walked), 0o600); err != nil {
				t.Fatal(err)
			}
			d, _ := newDeployer(f)
			f.rehearses(&rehearsal{pages: tc.pages})
			if tc.fail != "" {
				f.fail = []string{tc.fail}
			}

			if err := d.Release(context.Background(), next); err != nil {
				t.Fatalf("an unread walk refused the release: %v\n%s", err, f.out.String())
			}
			if !strings.Contains(f.out.String(), "result=pass") || !strings.Contains(f.out.String(), " pages=unread ") {
				t.Errorf("the verdict does not say the walk was unread:\n%s", f.out.String())
			}
			if data, err := os.ReadFile(f.machine.RehearsalPagesPath()); err == nil && strings.TrimSpace(string(data)) == walked {
				t.Error("the walk of an earlier release was left as this one's")
			}
		})
	}
}
