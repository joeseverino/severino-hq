package providers

import "time"

// What a container uses, from Docker's stats: the one reading of memory and
// CPU the glance and the runtime reading share.

// oneShotStats is the path of one container's single stats sample: Docker
// answers at once, where a plain stream=false read waits for a second sample.
func oneShotStats(id string) string {
	return "/containers/" + id + "/stats?stream=false&one-shot=true"
}

// The ways a container's memory in use is counted. Anonymous memory is what
// production's own reading takes (the cgroup's anon), so it is used wherever
// Docker reports it; elsewhere usage less the file cache the kernel can drop.
const (
	memoryAnon             = "anon"
	memoryLessInactiveFile = "usage less inactive file"
)

// memoryInUse is the memory one sample says the container holds, and how it
// was counted. A sample with no memory reading is nothing, counted no way.
func memoryInUse(sample dockerStats) (int64, string) {
	memory := sample.MemoryStats
	if anon, ok := memory.Stats["anon"]; ok {
		return max(0, anon), memoryAnon
	}
	if memory.Usage == 0 && len(memory.Stats) == 0 {
		return 0, ""
	}
	// Only cgroup v1 writes total_inactive_file, and there inactive_file is
	// the cgroup's own without its children's, as the Docker CLI reads it.
	inactive, v1 := memory.Stats["total_inactive_file"]
	if !v1 {
		inactive = memory.Stats["inactive_file"]
	}
	return max(0, memory.Usage-inactive), memoryLessInactiveFile
}

// cpuShare is a container's CPU between two samples as a share of one core
// (100 is one core busy throughout). One sample has no share: its counter is
// all the CPU since the container started. False when the two do not bound a
// window: either was taken at no known time, they are not in order, or the
// counter went back (the container restarted between them).
func cpuShare(earlier, later dockerStats) (float64, bool) {
	from, okFrom := sampledAt(earlier)
	to, okTo := sampledAt(later)
	used, before := later.CPUStats.CPUUsage.TotalUsage, earlier.CPUStats.CPUUsage.TotalUsage
	if !okFrom || !okTo || !to.After(from) || used < before {
		return 0, false
	}
	return float64(used-before) / float64(to.Sub(from).Nanoseconds()) * 100, true
}

// sampledAt is when the daemon took a sample; Docker writes the zero time for
// none.
func sampledAt(sample dockerStats) (time.Time, bool) {
	at, err := time.Parse(time.RFC3339Nano, sample.Read)
	if err != nil || at.Year() <= 1 {
		return time.Time{}, false
	}
	return at, true
}
