package host

import (
	"context"
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
)

// rehearsal is how the host answers a rehearsal's checks. Its zero value is a
// release that passes every one.
type rehearsal struct {
	notReady, noSocket, damaged, runsAnyJob, killed, liveRestarts bool
	planExit                                                      int
	marks                                                         int
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
		case strings.HasPrefix(call, planCall):
			return exec.CommandContext(ctx, "/bin/sh", "-c", `echo '{"mode":"plan"}'; exit "$1"`, "sh", strconv.Itoa(r.planExit))
		case call == "docker inspect --format {{.State.StartedAt}} "+Container:
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
		if strings.HasPrefix(call, "docker run") {
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

	if !strings.Contains(f.out.String(), "result=pass") {
		t.Errorf("no passing verdict in the deploy's log:\n%s", f.out.String())
	}
	f.before(t, planCall, "systemctl stop severino-hq-secrets.timer")
	f.before(t, "docker volume rm --force "+rehearsedVolume, "systemctl stop severino-hq-secrets.timer")
	f.before(t, "systemctl stop severino-hq-secrets.timer", "systemctl restart "+WebUnit)
	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != next {
		t.Errorf("the release is %q", data)
	}

	var web, plan string
	copied := false
	for _, call := range f.calls() {
		if !strings.HasPrefix(call, "docker run") {
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
