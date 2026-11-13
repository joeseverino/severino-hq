package hostcore

import (
	"encoding/json"
	"errors"
	"slices"
	"strconv"
	"strings"
	"time"
)

// MountTable is the kernel's table of what this process sees mounted.
const MountTable = "/proc/self/mounts"

// RealTypes are the filesystem types that hold a machine's files on disk. A
// container's overlay, a tmpfs or a kernel filesystem has no space of its
// own to run out of.
var RealTypes = []string{"ext4", "xfs", "btrfs", "zfs"}

// Filesystem is one real filesystem's space, as statfs states it: where it is
// mounted and nothing of its device or options.
type Filesystem struct {
	Mount      string `json:"mount"`
	FreeBytes  uint64 `json:"free_bytes"`
	TotalBytes uint64 `json:"total_bytes"`
}

// FilesystemReading is what root hands on: each real filesystem's space, and
// when it was read.
type FilesystemReading struct {
	ReadAt      string       `json:"read_at"`
	Filesystems []Filesystem `json:"filesystems"`
}

type mounted struct{ device, point, kind string }

// Mounts is the mount point of each real filesystem in a mount table (the
// fstab(5) format of /proc/self/mounts), one per device: the first mount of
// it a process can reach. A mount another covers, at its own point or above
// it, is not reachable, since statfs on its point answers for the one on top.
func Mounts(table []byte) []string {
	var entries []mounted
	for line := range strings.Lines(string(table)) {
		fields := strings.Fields(line)
		if len(fields) < 3 {
			continue
		}
		entries = append(entries, mounted{unescape(fields[0]), unescape(fields[1]), fields[2]})
	}
	var points []string
	seen := map[string]bool{}
	for i, entry := range entries {
		if !slices.Contains(RealTypes, entry.kind) || seen[entry.device] || covered(entry.point, entries[i+1:]) {
			continue
		}
		seen[entry.device] = true
		points = append(points, entry.point)
	}
	return points
}

// covered is whether a later mount sits on point or on a directory above it.
func covered(point string, later []mounted) bool {
	for _, over := range later {
		if over.point == point || over.point == "/" || strings.HasPrefix(point, over.point+"/") {
			return true
		}
	}
	return false
}

// unescape undoes the octal escapes the kernel writes in a mount table for a
// space, tab, newline or backslash in a name (\040, \011, \012, \134).
func unescape(field string) string {
	if !strings.Contains(field, `\`) {
		return field
	}
	var out strings.Builder
	for i := 0; i < len(field); i++ {
		if field[i] == '\\' && i+4 <= len(field) {
			if code, err := strconv.ParseUint(field[i+1:i+4], 8, 8); err == nil {
				out.WriteByte(byte(code))
				i += 3
				continue
			}
		}
		out.WriteByte(field[i])
	}
	return out.String()
}

// ReadFilesystems is each real filesystem in the mount table and its space,
// asked of statfs, as the document root hands on. A mount statfs cannot
// answer for is left out; a table with none it can is an error, so nothing
// stands in for a reading that was not taken.
func ReadFilesystems(table []byte, statfs func(path string) (free, total uint64, err error), now time.Time) ([]byte, error) {
	reading := FilesystemReading{ReadAt: now.UTC().Format(time.RFC3339), Filesystems: []Filesystem{}}
	for _, point := range Mounts(table) {
		free, total, err := statfs(point)
		if err != nil || total == 0 {
			continue
		}
		reading.Filesystems = append(reading.Filesystems, Filesystem{Mount: point, FreeBytes: free, TotalBytes: total})
	}
	if len(reading.Filesystems) == 0 {
		return nil, errors.New("statfs answered for no real filesystem")
	}
	return json.Marshal(reading)
}
