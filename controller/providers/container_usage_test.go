package providers

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// statsFixture is a stats answer from testdata, as Docker writes it, with its
// sample time and CPU counter set when given.
func statsFixture(t *testing.T, name, read string, cpu uint64) string {
	t.Helper()
	data, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatal(err)
	}
	if read != "" {
		doc["read"] = read
		doc["cpu_stats"].(map[string]any)["cpu_usage"].(map[string]any)["total_usage"] = cpu
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

func decodeStats(t *testing.T, body string) dockerStats {
	t.Helper()
	sample, err := decodeAnswer[dockerStats](json.RawMessage(body), "container stats")
	if err != nil {
		t.Fatal(err)
	}
	return sample
}

// Memory is the cgroup's anonymous memory where Docker reports it, as
// production's own reading takes it, and otherwise usage less the file cache
// the kernel can drop, labeled so.
func TestMemoryInUse(t *testing.T) {
	cases := []struct {
		name, body, basis string
		want              int64
	}{
		{"cgroup v2 reports anon", statsFixture(t, "docker-stats-one-shot-cgroup2.json", "", 0), memoryAnon, 153092096},
		{"cgroup v1 has no anon", statsFixture(t, "docker-stats-cgroup1.json", "", 0), memoryLessInactiveFile, 6537216 - 1048576},
		{"v2 without anon", `{"memory_stats":{"usage":1000,"stats":{"inactive_file":300}}}`, memoryLessInactiveFile, 700},
		{"cache larger than usage", `{"memory_stats":{"usage":100,"stats":{"inactive_file":300}}}`, memoryLessInactiveFile, 0},
		{"no memory reading", `{"memory_stats":{}}`, "", 0},
	}
	for _, c := range cases {
		used, basis := memoryInUse(decodeStats(t, c.body))
		if used != c.want || basis != c.basis {
			t.Errorf("%s: %d %q, want %d %q", c.name, used, basis, c.want, c.basis)
		}
	}
}

// A one-shot sample's counter is all the CPU since the container started, so
// no single sample is a share: only two that bound a window are.
func TestCPUShareNeedsTwoSamples(t *testing.T) {
	one := decodeStats(t, statsFixture(t, "docker-stats-one-shot-cgroup2.json", "", 0))
	if share, ok := cpuShare(one, one); ok {
		t.Errorf("one sample read as a share: %v", share)
	}
	at := func(read string, cpu uint64) dockerStats {
		return decodeStats(t, statsFixture(t, "docker-stats-one-shot-cgroup2.json", read, cpu))
	}
	cases := []struct {
		name           string
		earlier, later dockerStats
		want           float64
		bounds         bool
	}{
		{"half a core for a second", at("2026-10-31T12:00:00Z", 4_000_000_000), at("2026-10-31T12:00:01Z", 4_500_000_000), 50, true},
		{"two cores for a minute", at("2026-10-31T12:00:00Z", 0), at("2026-10-31T12:01:00Z", 120_000_000_000), 200, true},
		{"idle", at("2026-10-31T12:00:00Z", 9), at("2026-10-31T12:01:00Z", 9), 0, true},
		{"restarted between", at("2026-10-31T12:00:00Z", 9_000), at("2026-10-31T12:01:00Z", 10), 0, false},
		{"out of order", at("2026-10-31T12:01:00Z", 0), at("2026-10-31T12:00:00Z", 10), 0, false},
		{"no sample time", at("0001-01-01T00:00:00Z", 0), at("2026-10-31T12:00:00Z", 10), 0, false},
	}
	for _, c := range cases {
		share, ok := cpuShare(c.earlier, c.later)
		if share != c.want || ok != c.bounds {
			t.Errorf("%s: %v %v, want %v %v", c.name, share, ok, c.want, c.bounds)
		}
	}
}

// A running container's runtime record carries what its one-shot sample
// says it uses; one that is not running is not sampled.
func TestPortainerRuntimeCarriesUsage(t *testing.T) {
	docker := "/api/endpoints/1/docker"
	_, server := newPortainerAPI(t, map[string]string{
		docker + "/containers/json?all=1": `[{"Id":"a","Names":["/web-app-1"],"State":"running"},
			{"Id":"b","Names":["/web-job-1"],"State":"exited"}]`,
		docker + "/containers/a/json": `{"Image":"sha256:img1","State":{"StartedAt":"2026-10-31T11:00:00Z"}}`,
		docker + "/containers/b/json": `{"Image":"sha256:img2","State":{"StartedAt":"2026-10-30T11:00:00Z"}}`,
		docker + oneShotStats("a"):    statsFixture(t, "docker-stats-one-shot-cgroup2.json", "", 0),
	})
	r := portainerRegistry(t, server.URL, runtime.Environment{})
	found, err := r.portainerRuntime(t.Context(), portainerSite{ConnectionRef: "portainer-example", EnvironmentID: 1, Host: "hq-node"})
	if err != nil || len(found) != 2 {
		t.Fatalf("%v %v", found, err)
	}
	running, stopped := found[0].(PortainerRuntimeRecord), found[1].(PortainerRuntimeRecord)
	if running.MemoryUsage != 153092096 || running.MemoryUsageBasis != memoryAnon ||
		running.CPUTime != 4815162342 || running.UsageReadAt != "2026-10-31T12:00:00.123456789Z" {
		t.Errorf("running: %+v", running)
	}
	if stopped.MemoryUsage != 0 || stopped.MemoryUsageBasis != "" || stopped.CPUTime != 0 || stopped.UsageReadAt != "" {
		t.Errorf("stopped: %+v", stopped)
	}
}
